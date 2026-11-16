"""Owned loopback host for the Harness integration; no legacy UI/model startup.

Copyright (c) yangshaopeng. Licensed under Apache-2.0.
"""
import argparse
import hmac
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--core-root', required=True)
    parser.add_argument('--state-dir', required=True)
    parser.add_argument('--instance-id', required=True)
    parser.add_argument('--cases-root')
    args = parser.parse_args()
    core = Path(args.core_root).resolve()
    package = core / 'dpf_agent'
    if not (package / 'dpf_agent/professional/service.py').is_file():
        parser.error('core-root must contain dpf_agent/dpf_agent/professional/service.py')
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(package))
    import uvicorn
    from fastapi import HTTPException, Request
    from dpf_agent.professional.api import create_app
    from dpf_agent.professional.service import ProfessionalService, atomic_text

    root = Path(args.state_dir).resolve()
    token = secrets.token_urlsafe(32)
    shutdown_token = secrets.token_urlsafe(32)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(('127.0.0.1', 0))
    service = None
    try:
        service = ProfessionalService(root, args.cases_root)
        app = create_app(service, token, close_on_shutdown=True)
        server = uvicorn.Server(uvicorn.Config(app, log_level='warning', access_log=False))
        code = package / 'dpf_agent'
        code_entries = [[p.relative_to(code).as_posix(), hashlib.sha256(p.read_bytes()).hexdigest()]
                        for p in sorted(code.rglob('*.py')) if '__pycache__' not in p.parts]
        identity = {
            'pid': os.getpid(), 'parent_pid': os.getppid(), 'integration_instance': args.instance_id,
            'core_root': str(core), 'state_dir': str(root), 'cases_root': str(service.cases_root),
            'core_python_sha256': hashlib.sha256(json.dumps(code_entries, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest(),
            'host_script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        }

        @app.get('/_integration/identity', include_in_schema=False)
        async def integration_identity():
            return identity

        @app.post('/_integration/shutdown', include_in_schema=False)
        async def integration_shutdown(request: Request):
            # The ordinary Bearer middleware also applies. This second secret is
            # kept in the owned connection file, never in a model-visible tool.
            supplied = request.headers.get('x-tianshu-control', '')
            if not hmac.compare_digest(supplied.encode(), shutdown_token.encode()):
                raise HTTPException(403, 'owned integration control required')
            if await request.json() != {}:
                raise HTTPException(422, 'empty JSON object required')
            server.should_exit = True
            return {'stopping': True}

        atomic_text(root / 'local-service.json', json.dumps({
            'url': f'http://127.0.0.1:{sock.getsockname()[1]}',
            'token': token, 'service': 'tianshu-professional',
            **identity,
            'shutdown_token': shutdown_token,
        }, indent=2))
        server.run(sockets=[sock])
    finally:
        sock.close()
        if service is not None:
            service.close()


if __name__ == '__main__':
    main()
