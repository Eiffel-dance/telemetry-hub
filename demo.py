from app import Telemetry

t = Telemetry(lambda: 123.0)
t.inc('requests', labels=(('route', '/api'),))
t.inc('requests', service='web')
t.observe('latency_ms', 12.5)
t.observe('latency_ms', 7.5, service='web')
t.start('request')
t.finish('request')
t.start('checkout', service='web')
t.finish('checkout', error='timeout', service='web')
print(t.json())
print(t.spans('error'))
