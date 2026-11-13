"""Generate an audit without exporting source keys or location coordinates."""
import json
from reporter import Reporter, SOURCES, read_source

r = Reporter('/var/lib/startag-google-reporter')
report = r.status()
report['accounts'] = []
for account, directory in enumerate(SOURCES, 1):
    email, devices = read_source(directory)
    rows = []
    for d, points in devices:
        counts = dict(r.db.execute('SELECT status,count(*) FROM outbox WHERE account=? AND sn=? GROUP BY status', (account, d['sn'])))
        storage = dict(r.db.execute("SELECT json_extract(result,'$.storage_target'),count(*) FROM outbox WHERE account=? AND sn=? AND status='sent' GROUP BY 1", (account, d['sn'])))
        rows.append(dict(name=d.get('name'), sn=d['sn'], history_count=len(points), sent=counts.get('sent',0), pending=counts.get('pending',0), storage=storage))
    report['accounts'].append(dict(account=account, email=email, devices=rows))
print(json.dumps(report, ensure_ascii=False, indent=2))
