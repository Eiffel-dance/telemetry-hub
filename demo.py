from app import Telemetry
t=Telemetry(lambda:123.0); t.inc('requests',labels=(('service','api'),)); t.observe('latency_ms',12.5); t.start('request'); t.finish('request'); print(t.json())
