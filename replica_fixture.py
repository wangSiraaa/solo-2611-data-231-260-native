#!/usr/bin/env python3
"""Task input infrastructure: tiny primary/replica data producer, not a solution."""
import argparse,json,sqlite3
from pathlib import Path
SCHEMA='CREATE TABLE meta(cluster_id TEXT NOT NULL,generation INTEGER NOT NULL,applied_seq INTEGER NOT NULL);CREATE TABLE accounts(id TEXT PRIMARY KEY,balance INTEGER NOT NULL);CREATE TABLE history(seq INTEGER PRIMARY KEY,amount INTEGER NOT NULL,a_balance INTEGER NOT NULL,b_balance INTEGER NOT NULL);'
def connect(path):
 c=sqlite3.connect(path,timeout=5);c.execute('PRAGMA journal_mode=WAL');return c
def initialize(root):
 root.mkdir(parents=True,exist_ok=True)
 for node in ['primary','replica']:
  p=root/(node+'.db')
  if p.exists():raise SystemExit('refusing to overwrite existing database')
  c=connect(p);c.executescript(SCHEMA);c.execute('INSERT INTO meta VALUES(?,?,?)',('demo-cluster',1,0));c.executemany('INSERT INTO accounts VALUES(?,?)',[('a',600),('b',400)]);c.commit();c.close()
 (root/'cluster.json').write_text(json.dumps({'cluster_id':'demo-cluster','generation':1}))
def receipt(row):return dict(zip(['cluster_id','generation','commit_seq'],row))
def commit(root,amount):
 c=connect(root/'primary.db');c.execute('BEGIN IMMEDIATE');cluster,gen,seq=c.execute('SELECT * FROM meta').fetchone();seq+=1
 c.execute("UPDATE accounts SET balance=balance-? WHERE id='a'",(amount,));c.execute("UPDATE accounts SET balance=balance+? WHERE id='b'",(amount,));a,b=[x[0] for x in c.execute('SELECT balance FROM accounts ORDER BY id')]
 c.execute('INSERT INTO history VALUES(?,?,?,?)',(seq,amount,a,b));c.execute('UPDATE meta SET applied_seq=?',(seq,));c.commit();c.close();return receipt((cluster,gen,seq))
def replicate(root):
 src=connect(root/'primary.db');src.execute('BEGIN');m=src.execute('SELECT * FROM meta').fetchone();a=src.execute('SELECT * FROM accounts').fetchall();h=src.execute('SELECT * FROM history').fetchall();src.commit();src.close()
 dst=connect(root/'replica.db');dst.execute('BEGIN IMMEDIATE')
 for t in ['meta','accounts','history']:dst.execute('DELETE FROM '+t)
 dst.execute('INSERT INTO meta VALUES(?,?,?)',m);dst.executemany('INSERT INTO accounts VALUES(?,?)',a);dst.executemany('INSERT INTO history VALUES(?,?,?,?)',h);dst.commit();dst.close();return receipt(m)
def reset(root,node,generation):
 c=connect(root/(node+'.db'));c.execute('BEGIN IMMEDIATE');c.execute('UPDATE meta SET generation=?,applied_seq=0',(generation,));c.execute('DELETE FROM history');c.execute("UPDATE accounts SET balance=CASE id WHEN 'a' THEN 600 ELSE 400 END");c.commit();c.close()
def main():
 p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True);sub=p.add_subparsers(dest='cmd',required=True);sub.add_parser('init');s=sub.add_parser('commit');s.add_argument('--amount',type=int,required=True);sub.add_parser('replicate');s=sub.add_parser('reset-node');s.add_argument('--node',choices=['primary','replica'],required=True);s.add_argument('--generation',type=int,required=True);a=p.parse_args()
 if a.cmd=='init':initialize(a.root);out={'initialized':True}
 elif a.cmd=='commit':out=commit(a.root,a.amount)
 elif a.cmd=='replicate':out=replicate(a.root)
 else:reset(a.root,a.node,a.generation);out={'reset':a.node,'generation':a.generation}
 print(json.dumps(out))
if __name__=='__main__':main()
