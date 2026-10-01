import json,time
class Telemetry:
    def __init__(self,clock=time.time): self.clock=clock; self.counters={}; self.samples={}; self.spans={}
    def inc(self,name,value=1,labels=()): self.counters[(name,tuple(sorted(labels)))]=self.counters.get((name,tuple(sorted(labels))),0)+value
    def observe(self,name,value,labels=()): self.samples.setdefault((name,tuple(sorted(labels))),[]).append(value)
    def start(self,span,parent=None): self.spans[span]={"parent":parent,"start":self.clock(),"end":None,"error":None}
    def finish(self,span,error=None): self.spans[span]["end"]=self.clock(); self.spans[span]["error"]=error
    def snapshot(self): return {"counters":[{"name":n,"labels":list(l),"value":v} for (n,l),v in sorted(self.counters.items())],"samples":[{"name":n,"labels":list(l),"values":v} for (n,l),v in sorted(self.samples.items())],"spans":self.spans}
    def json(self): return json.dumps(self.snapshot(),sort_keys=True,separators=(",",":"))
