from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
import json, io, time, webbrowser
from engine import load_csv, calculate, FORMULAS, DRIVERS
BASE=Path(__file__).resolve().parent
class Handler(BaseHTTPRequestHandler):
    def send(self, data, kind='application/json', status=200):
        if isinstance(data,str): data=data.encode()
        self.send_response(status); self.send_header('Content-Type',kind); self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data)
    def do_GET(self):
        if self.path=='/': return self.send((BASE/'index.html').read_bytes(),'text/html; charset=utf-8')
        if self.path=='/config': return self.send(json.dumps({'formulas':{k:v[0] for k,v in FORMULAS.items()},'drivers':{k:v[0] for k,v in DRIVERS.items()}}))
        self.send('Not found','text/plain',404)
    def do_POST(self):
        if self.path!='/calculate': return self.send('Not found','text/plain',404)
        try:
            size=int(self.headers.get('Content-Length',0));
            if size>15_000_000: raise ValueError('CSV too large (15 MB maximum).')
            req=json.loads(self.rfile.read(size)); start=time.perf_counter()
            d=load_csv(io.StringIO(req['csv'])) if req.get('csv') else load_csv(BASE/'demo.csv')
            steps=max(4,min(32,int(req.get('steps',18)))); seeds=max(4,min(16,int(req.get('seeds',12))))
            weights={k:max(0,min(3,float(v))) for k,v in req.get('weights',{}).items() if k in DRIVERS}
            out=calculate(d,req.get('formulas',list(FORMULAS)),weights,steps,seeds); out['seconds']=round(time.perf_counter()-start,3); out['source']='Uploaded CSV' if req.get('csv') else 'Synthetic NQ demo'; self.send(json.dumps(out,allow_nan=False,separators=(',',':')))
        except Exception as e: self.send(json.dumps({'error':str(e)}),status=400)
if __name__=='__main__':
    server=ThreadingHTTPServer(('127.0.0.1',8765),Handler); print('Open http://127.0.0.1:8765 — Ctrl+C to stop'); webbrowser.open('http://127.0.0.1:8765'); server.serve_forever()