"""Optional loopback Ollama runtime for Tianzhi5. Copyright (c) yangshaopeng.

No model download, profile mutation, firewall change or cloud fallback.
Start with explicitly selected existing runtime and model directory; this does
not change the Harness default provider. Stop the serving console with Ctrl+C.
"""
from pathlib import Path
import argparse,json,os,socket,subprocess,urllib.request

def main():
 p=argparse.ArgumentParser(description='天智五号可选本地模型服务')
 p.add_argument('command',choices=['serve','probe'])
 p.add_argument('--port',type=int,default=11435)
 p.add_argument('--exe',help='Existing official ollama.exe, required for serve')
 p.add_argument('--models-root',help='Existing Ollama model directory, required for serve')
 p.add_argument('--model',default='qwen2.5:7b')
 a=p.parse_args()
 if not 1024<=a.port<=65535:p.error('port must be 1024..65535')
 if a.command=='probe':
  with urllib.request.urlopen(f'http://127.0.0.1:{a.port}/v1/models',timeout=5) as r:data=json.load(r)
  names=[m['id'] for m in data['data']]
  print(json.dumps({'endpoint':f'http://127.0.0.1:{a.port}/v1','models':names,'selected_model_present':a.model in names,'tool_calling_tested':False,'GUI_tested':False},ensure_ascii=False,indent=2))
  return 0 if a.model in names else 1
 if not a.exe or not a.models_root:p.error('serve requires --exe and --models-root')
 exe=Path(a.exe).resolve();models=Path(a.models_root).resolve()
 if not exe.is_file() or exe.name.lower()!='ollama.exe' or not (models/'manifests').is_dir() or not (models/'blobs').is_dir():p.error('select an existing runtime executable and model directory')
 with socket.socket() as s:
  if s.connect_ex(('127.0.0.1',a.port))==0:p.error('port already occupied; no existing process is adopted or stopped')
 env=dict(os.environ,OLLAMA_HOST=f'127.0.0.1:{a.port}',OLLAMA_MODELS=str(models),OLLAMA_NO_CLOUD='1',OLLAMA_NUM_PARALLEL='1',OLLAMA_CONTEXT_LENGTH='8192')
 print('Local model endpoint: '+f'http://127.0.0.1:{a.port}/v1',flush=True)
 return subprocess.call([str(exe),'serve'],env=env)

if __name__=='__main__':raise SystemExit(main())
