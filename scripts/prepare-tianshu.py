"""Prepare an existing Tianshu USB source for a new Windows computer.

2026-10-28, codex. Copies files and creates a local plugin bundle only.
Does not install software, modify a DSH profile, register COM, or run a solver.
"""
from pathlib import Path
import argparse, hashlib, io, json, os, re, shutil, tarfile

SKIP = {'__pycache__', '.pytest_cache', '.git'}

def sha(data):
    return hashlib.sha256(data).hexdigest()

def prepare(source, target, state):
    source, target, state = (Path(p).resolve() for p in (source,target,state))
    if source == target or source.is_relative_to(target) or target.is_relative_to(source):
        raise ValueError('Source and target must be separate directory trees.')
    if not (source/'dpf_agent/professional/service.py').is_file():
        raise ValueError('Choose the USB folder containing dpf_agent/professional/service.py.')
    plugin=source/'integrations/deepseek-harness'
    required=['index.js','schemas.js','service_host.py','cordis.patch.yml','package.json','README.md']
    for name in required:
        if not (plugin/name).is_file():raise ValueError('Missing plugin file: '+name)
    if not (source/'cases/case4/Master.dss').is_file():raise ValueError('Missing cases/case4/Master.dss')
    if not (source/'requirements-windows-py313.lock.txt').is_file():raise ValueError('Missing Python dependency lock file.')
    if not (source/'LICENSE').is_file():raise ValueError('Keep the project LICENSE next to this script.')
    plan=[]
    def add(src,dst):
        if src.is_symlink():raise ValueError('Linked input is not accepted: '+str(src))
        data=src.read_bytes()
        if dst.exists() and (not dst.is_file() or dst.read_bytes()!=data):
            raise ValueError('Existing target differs; use a new empty target directory: '+str(dst))
        plan.append((src,dst,data))
    for srcdir,relative in [(source/'dpf_agent','dpf_agent/dpf_agent'),(source/'cases','dpf_agent/cases'),(source/'integrations','integrations'),(source/'测试算例','测试算例')]:
        if not srcdir.is_dir():raise ValueError('Missing directory: '+str(srcdir))
        for base,dirs,names in os.walk(srcdir,followlinks=False):
            for d in dirs:
                if (Path(base)/d).is_symlink():raise ValueError('Linked directory is not accepted.')
            dirs[:]=[d for d in dirs if d not in SKIP]
            for name in sorted(names):
                src=Path(base)/name
                if src.suffix.lower() not in {'.pyc','.pyo'}:
                    add(src,target/relative/src.relative_to(srcdir))
    add(source/'requirements-windows-py313.lock.txt',target/'dpf_agent/requirements-windows-py313.lock.txt')
    for name in ['LICENSE','NOTICE']:
        if (source/name).is_file():add(source/name,target/name)
    package=json.loads((plugin/'package.json').read_text('utf-8'))
    if package.get('name')!='@yangshaopeng/tianshu-harness' or package.get('version')!='0.2.4-acceptance':
        raise ValueError('This guide requires the reviewed 0.2.4-acceptance plugin source.')
    config={'stateDir':str(state),'python':str(target/'dpf_agent/.venv/Scripts/python.exe'),
            'coreRoot':str(target),'casesRoot':str(target/'dpf_agent/cases')}
    patch=(plugin/'cordis.patch.yml').read_text('utf-8')
    pattern=re.compile(r"(?m)^(?P<head>[ ]*name:[ ]*['\"]?@yangshaopeng/tianshu-harness['\"]?[ ]*\r?\n)(?P<indent>[ ]*)config:[ ]*\{[ ]*\}[ ]*$")
    if len(list(pattern.finditer(patch)))!=1:
        raise ValueError('Plugin patch is not the expected empty public config; preserve it for review.')
    private_patch=pattern.sub(lambda m:m['head']+m['indent']+'config: '+json.dumps(config,ensure_ascii=False),patch)
    members={}
    for src in sorted(plugin.rglob('*')):
        if src.is_symlink():raise ValueError('Linked plugin member is not accepted.')
        if src.is_file() and not SKIP.intersection(src.relative_to(plugin).parts) and src.suffix not in {'.pyc','.pyo'}:
            members[src.relative_to(plugin).as_posix()]=src.read_bytes()
    members['cordis.patch.yml']=private_patch.encode('utf-8')
    members['LICENSE']=(source/'LICENSE').read_bytes()
    brand=source/'integrations/tianzhi-brand'
    if not (brand/'client.js').is_file():raise ValueError('Missing Tianzhi branding client.')
    brand_meta=json.loads((brand/'package.json').read_text('utf-8'))
    if brand_meta.get('name')!='@yangshaopeng/tianzhi-brand' or brand_meta.get('version')!='0.1.3':
        raise ValueError('Brand package version mismatch.')
    brand_members={p.relative_to(brand).as_posix():p.read_bytes() for p in brand.rglob('*') if p.is_file() and not SKIP.intersection(p.parts)}
    brand_members['LICENSE']=(source/'LICENSE').read_bytes()
    brand_output=target/'deployment/tianzhi-brand-0.1.3-local.tgz'
    if brand_output.exists():raise ValueError('A prepared brand bundle already exists.')
    output=target/'deployment/tianshu-harness-0.2.4-acceptance-local.tgz'
    receipt=target/'deployment/preparation_receipt.json'
    if output.exists() or receipt.exists():raise ValueError('A prepared bundle already exists. Preserve it and use a new empty target directory.')
    # All source, config, and conflict checks above complete before copying.
    copied=[]
    for src,dst,data in plan:
        dst.parent.mkdir(parents=True,exist_ok=True)
        if not dst.exists():
            with dst.open('xb') as stream:stream.write(data)
        if dst.read_bytes()!=data or src.read_bytes()!=data:raise ValueError('Copy verification failed.')
        copied.append({'source':src.relative_to(source).as_posix(),'destination':dst.relative_to(target).as_posix(),'bytes':len(data),'sha256':sha(data)})
    output.parent.mkdir(parents=True,exist_ok=True)
    with output.open('xb') as raw:
        with tarfile.open(fileobj=raw,mode='w:gz') as archive:
            for name,data in sorted(members.items()):
                item=tarfile.TarInfo('package/'+name)
                item.size=len(data);item.mode=0o644;item.mtime=0
                archive.addfile(item,io.BytesIO(data))
    with tarfile.open(output,'r:gz') as archive:
        if len(archive.getmembers())!=len(members):raise ValueError('Bundle inventory mismatch.')
        for item in archive.getmembers():
            if not item.isfile() or archive.extractfile(item).read()!=members[item.name[len('package/'):]]:
                raise ValueError('Bundle verification failed.')
    with brand_output.open('xb') as raw:
        with tarfile.open(fileobj=raw,mode='w:gz') as archive:
            for name,data in sorted(brand_members.items()):
                item=tarfile.TarInfo('package/'+name);item.size=len(data);item.mode=0o644;item.mtime=0
                archive.addfile(item,io.BytesIO(data))
    with tarfile.open(brand_output,'r:gz') as archive:
        if len(archive.getmembers())!=len(brand_members):raise ValueError('Brand inventory mismatch.')
        for item in archive.getmembers():
            if not item.isfile() or archive.extractfile(item).read()!=brand_members[item.name[len('package/'):]]:
                raise ValueError('Brand bundle verification failed.')
    result={'date':'2026-10-28','author':'codex','work':'USB migration file preparation',
            'source':str(source),'target':str(target),'config':config,'copied_files':copied,
            'bundle':str(output),'bundle_sha256':sha(output.read_bytes()),'bundle_members':len(members),
            'brand_bundle':str(brand_output),'brand_bundle_sha256':sha(brand_output.read_bytes()),'brand_bundle_members':len(brand_members),
            'file_preparation_verified':True,'software_installed':False,'DSH_profile_changed':False,
            'COM_registered_by_script':False,'solver_tasks_created':0,'new_computer_acceptance':False}
    receipt.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n','utf-8')
    return result

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',default=str(Path(__file__).resolve().parent))
    parser.add_argument('--target',default=r'C:\Tianzhi5\agent')
    parser.add_argument('--state-dir',default=str(Path(os.environ.get('LOCALAPPDATA',str(Path.home()/'AppData/Local')))/'Tianzhi5/harness-state'))
    args=parser.parse_args()
    try:
        result=prepare(args.source,args.target,args.state_dir)
        print(json.dumps({k:result[k] for k in ['target','bundle','brand_bundle','bundle_sha256','bundle_members','file_preparation_verified','software_installed','new_computer_acceptance']},ensure_ascii=False,indent=2))
    except (OSError,ValueError,KeyError) as exc:
        parser.exit(2,'Preparation stopped: '+str(exc)+'\n')

if __name__=='__main__':main()
