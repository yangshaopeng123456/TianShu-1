"""Authenticated loopback API for the independent professional core."""
from __future__ import annotations

from contextlib import asynccontextmanager
import hmac
import json

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from .contracts import ContractError
from .jobs import RequestConflict
from .service import PROFESSIONAL_VERSION, ProfessionalService


def create_app(service: ProfessionalService, access_token: str, *, close_on_shutdown=False):
    if not isinstance(access_token, str) or len(access_token) < 24:
        raise ValueError("A nonempty local access token (at least 24 characters) is required")

    @asynccontextmanager
    async def lifespan(app):
        yield
        if close_on_shutdown:
            await run_in_threadpool(service.close)

    app = FastAPI(title="Tianshu professional core", version=PROFESSIONAL_VERSION, lifespan=lifespan)

    @app.middleware("http")
    async def local_owner(request: Request, call_next):
        from starlette.responses import JSONResponse
        host = request.url.hostname
        if host not in {"127.0.0.1", "localhost", "testserver"}:
            return JSONResponse({"detail":"loopback host required"}, status_code=403)
        expected = "Bearer " + access_token
        supplied = request.headers.get("authorization", "")
        if not hmac.compare_digest(supplied.encode(), expected.encode()):
            return JSONResponse({"detail":"local service token required"}, status_code=401)
        if request.method == "POST" and request.headers.get("content-type", "").split(";", 1)[0] != "application/json":
            return JSONResponse({"detail":"application/json required"}, status_code=415)
        return await call_next(request)

    @app.exception_handler(ContractError)
    async def contract_error(request, exc):
        from starlette.responses import JSONResponse
        return JSONResponse(exc.to_dict(), status_code=422)

    @app.exception_handler(RequestConflict)
    async def conflict(request, exc):
        from starlette.responses import JSONResponse
        return JSONResponse({"code":"request_id_conflict", "message":str(exc)}, status_code=409)

    @app.exception_handler(KeyError)
    async def missing(request, exc):
        from starlette.responses import JSONResponse
        return JSONResponse({"code":"not_found"}, status_code=404)

    @app.get("/v1/health")
    async def health():
        # Authenticated readiness only. Never enumerate snapshots or acquire a
        # worker thread: slow model verification must not amplify startup polls.
        return {"service":"tianshu-professional", "version":PROFESSIONAL_VERSION, "ready":True}

    @app.get("/v1/capabilities")
    def capabilities():
        return service.capabilities()

    @app.get("/v1/models")
    def models():
        return service.models()

    @app.post("/v1/models/import")
    async def import_model(request: Request):
        # The HTTP adapter accepts file content, never a server filesystem path.
        # Bound the wire body while reading; the model store also limits decoded
        # UTF-8 source bytes and file count independently.
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > 32 * 1024 * 1024:
                raise HTTPException(413, "model import exceeds the wire budget")
            body.extend(chunk)
        try:
            payload = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeError):
            raise HTTPException(400, "invalid UTF-8 JSON") from None
        return await run_in_threadpool(service.import_model, payload)

    @app.get("/v1/models/{snapshot_id}")
    def inspect_model(snapshot_id: str):
        return service.inspect_model(snapshot_id)

    @app.post("/v1/studies", status_code=202)
    async def submit_study(request: Request):
        body=bytearray()
        async for chunk in request.stream():
            if len(body)+len(chunk)>2*1024*1024:raise HTTPException(413,"study exceeds 2 MiB")
            body.extend(chunk)
        try:payload=json.loads(body.decode("utf-8"))
        except (ValueError,UnicodeError):raise HTTPException(400,"invalid JSON") from None
        return await run_in_threadpool(service.submit_study,payload)

    @app.get("/v1/studies/{study_id}")
    def study(study_id: str):
        return service.get_study(study_id)

    @app.get("/v1/studies/by-request/{request_id}")
    def study_by_request(request_id: str):
        return service.studies.by_request(request_id)

    @app.post("/v1/studies/{study_id}/cancel")
    async def cancel_study(study_id: str,request:Request):
        try:body=await request.json()
        except (ValueError,UnicodeError):raise HTTPException(400,"invalid JSON") from None
        if body!={}:raise HTTPException(422,"cancel requires an empty object")
        return await run_in_threadpool(service.cancel_study,study_id)

    @app.get("/v1/studies/{study_id}/artifacts/{name}")
    def study_artifact(study_id: str,name: str):
        data=service.studies.artifact(study_id,name)
        media={"report.html":"text/html","points.csv":"text/csv","result.json":"application/json"}[name]
        return Response(data,media_type=media,headers={"X-Content-Type-Options":"nosniff","Content-Disposition":f'attachment; filename="{name}"'})

    @app.get("/v1/studies/{study_id}/report.pdf")
    def study_pdf(study_id: str):
        return Response(service.export_study_pdf(study_id),media_type="application/pdf",headers={
            "Content-Disposition":'attachment; filename="report.pdf"',"X-Content-Type-Options":"nosniff"})

    @app.post("/v1/compare-scenarios")
    async def compare_scenarios(request:Request):
        body=await request.body()
        if len(body)>1024:raise HTTPException(413,"comparison request too large")
        try:p=json.loads(body)
        except ValueError:raise HTTPException(400,"invalid JSON") from None
        if not isinstance(p,dict) or set(p)!={"left_task_id","right_task_id"} or any(not isinstance(v,str) for v in p.values()):raise HTTPException(422,"two explicit task IDs required")
        return await run_in_threadpool(service.compare_scenarios,p['left_task_id'],p['right_task_id'])

    @app.post("/v1/jobs", status_code=202)
    async def submit(request: Request):
        if len(await request.body()) > 256 * 1024:
            raise HTTPException(413, "request exceeds the local contract budget")
        try:
            payload = await request.json()
        except (ValueError, UnicodeError):
            raise HTTPException(400, "invalid JSON") from None
        return await run_in_threadpool(service.submit, payload)

    @app.get("/v1/jobs/{task_id}")
    def task(task_id: str):
        return service.get_task(task_id)

    @app.get("/v1/jobs/{task_id}/events")
    def events(task_id: str, after: int = Query(0, ge=0)):
        return service.events(task_id, after)

    @app.post("/v1/jobs/{task_id}/cancel")
    async def cancel(task_id: str, request: Request):
        try:
            payload = await request.json()
        except (ValueError, UnicodeError):
            raise HTTPException(400, "invalid JSON") from None
        if payload != {}:
            raise HTTPException(422, "cancel accepts an empty object")
        return await run_in_threadpool(service.cancel, task_id)

    @app.get("/v1/jobs/{task_id}/result")
    def result(task_id: str):
        value = service.result(task_id)
        if value is None:
            raise HTTPException(409, "job has no committed result")
        return value

    @app.post("/v1/jobs/{task_id}/compare-reference")
    async def compare_reference(task_id: str, request: Request):
        body = await request.body()
        if len(body) > 4 * 1024 * 1024:
            raise HTTPException(413, "reference exceeds the local contract budget")
        try:
            reference = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeError):
            raise HTTPException(400, "invalid UTF-8 reference JSON") from None
        return await run_in_threadpool(service.compare_reference, task_id, reference)

    @app.post("/v1/jobs/{task_id}/compare-reference-collection")
    async def compare_reference_collection(task_id: str, request: Request):
        # Separate bounded collection contract; the legacy 4 MiB/10000-point
        # single-reference endpoint retains its original limits.
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > 8 * 1024 * 1024:
                raise HTTPException(413, "reference collection exceeds the 8 MiB contract budget")
            body.extend(chunk)
        try:
            collection = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeError):
            raise HTTPException(400, "invalid UTF-8 reference collection JSON") from None
        return await run_in_threadpool(service.compare_reference_collection, task_id, collection)

    @app.get("/v1/jobs/{task_id}/report-bundle")
    def report_bundle(task_id: str):
        data = service.export_report_bundle(task_id)
        return Response(data, media_type="application/zip", headers={
            "Content-Disposition":'attachment; filename="tianshu-report.zip"',
            "X-Content-Type-Options":"nosniff"})

    @app.get("/v1/jobs/{task_id}/report.pdf")
    def report_pdf(task_id: str):
        return Response(service.export_report_pdf(task_id), media_type="application/pdf", headers={
            "Content-Disposition":'attachment; filename="report.pdf"', "X-Content-Type-Options":"nosniff"})

    @app.get("/v1/jobs/{task_id}/artifacts/{name}")
    def artifact(task_id: str, name: str):
        data = service.artifact_bytes(task_id, name)
        suffix = name.rsplit(".", 1)[1]
        media_type = {"html":"text/html", "json":"application/json", "csv":"text/csv"}[suffix]
        return Response(data, media_type=media_type, headers={
            "Content-Disposition":f'attachment; filename="{name}"', "X-Content-Type-Options":"nosniff"})

    return app
