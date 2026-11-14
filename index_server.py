import argparse,json,pathlib,threading
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
p=argparse.ArgumentParser();p.add_argument('--rows',required=True);p.add_argument('--ready',required=True);p.add_argument('--change-after',type=int,default=0);a=p.parse_args()
rows=json.loads(pathlib.Path(a.rows).read_text());byid={r['id']:r for r in rows};snapshot='snapshot-1';calls=[];lock=threading.Lock()
class H(BaseHTTPRequestHandler):
 def log_message(self,*a):pass
 def reply(self,x,status=200):
  b=json.dumps(x).encode();self.send_response(status);self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b)
 def do_GET(self):
  if self.path=='/info':return self.reply({'snapshot':snapshot,'count':len(rows),'columns':['x','y']})
  if self.path=='/__inspect':return self.reply({'calls':calls,'snapshot':snapshot})
  self.reply({'error':'unknown'},404)
 def do_POST(self):
  global snapshot
  try:
   r=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
   with lock:
    if a.change_after and len(calls)>=a.change_after:snapshot='snapshot-2'
    if r.get('snapshot')!=snapshot:return self.reply({'error':'snapshot_changed','snapshot':snapshot},409)
    if self.path=='/scan':
     col=r['column'];assert col in ('x','y');offset=r.get('cursor',0);limit=r['limit'];assert type(offset)is int and type(limit)is int and offset>=0 and 1<=limit<=3
     ordered=sorted(rows,key=lambda x:(-x[col],x['id'].encode()));part=ordered[offset:offset+limit];end=offset+len(part)
     calls.append({'op':'scan','column':col,'cursor':offset,'returned_ids':[x['id'] for x in part]})
     return self.reply({'snapshot':snapshot,'items':[{'id':x['id'],'score':x[col]} for x in part],'next':end,'exhausted':end>=len(ordered)})
    if self.path=='/lookup':
     ids=r['ids'];assert len(ids)<=4 and all(x in byid for x in ids);calls.append({'op':'lookup','ids':ids})
     return self.reply({'snapshot':snapshot,'items':[byid[i] for i in ids]})
    self.reply({'error':'unknown'},404)
  except (ValueError,KeyError,AssertionError,TypeError):self.reply({'error':'bad request'},400)
s=ThreadingHTTPServer(('127.0.0.1',0),H);pathlib.Path(a.ready).write_text(json.dumps({'base_url':f'http://127.0.0.1:{s.server_port}'}));s.serve_forever()
