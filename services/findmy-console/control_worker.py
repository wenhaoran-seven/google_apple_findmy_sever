"""Independent collector adapter. The HTTP process never contacts collectors."""
import asyncio, json, logging, time
import app as host
import control_store as store
from fastapi import HTTPException

log=logging.getLogger('findmy.control')

def execute(job):
    try:
        a=host.account(job['account'])
        result=host.relay(a,job['path'],store.unpack(job['payload']),job['method'])
        if a['platform']=='google' and job['path']=='devices' and job['method']=='POST':
            with host.db.connect() as c:c.execute("INSERT OR IGNORE INTO burn_registry VALUES(?,'unburned',NULL,?,'new Google identity')",(result['sn'],time.time()))
        store.finish(job['id'],result)
    except HTTPException as e:store.finish(job['id'],{'detail':e.detail},e.status_code)
    except Exception:
        log.exception('Control task failed id=%s',job['id'])
        store.finish(job['id'],{'detail':'后台操作异常，结果未确认，请核对后再操作'},502)

def sync_account(a):
    data=host.collector(a)
    store.save_projection(data)
    if not data.get('available'):return
    wanted=store.names()
    for d in data['devices']:
        key='sn:'+str(d['sn']) if d.get('sn') else a['id']+'~'+d['id']
        target=wanted.get(key)
        if not target or d['name']==target['name']:continue
        try:
            host.relay(a,'devices/'+d['id']+'/rename',{'name':target['name']})
        except Exception:
            # Idempotent metadata reconciliation only. Never repeat register/delete.
            with store.connect() as c:c.execute('UPDATE names SET error=? WHERE device_key=? AND revision=?',('部分采集端名称尚未同步，将自动重试',key,target['revision']))

async def account_loop(ident):
    next_sync=0
    while True:
        try:
            job=await asyncio.to_thread(store.claim,ident)
            if job:
                await asyncio.to_thread(execute,job)
                next_sync=0
            elif time.monotonic()>=next_sync:
                a=next((a for a in host.accounts() if a['id']==ident),None)
                if a:await asyncio.to_thread(sync_account,a)
                # Positions are read directly from locations.sqlite3 by the web;
                # inventory polling need not repeatedly scan collector histories.
                next_sync=time.monotonic()+60
        except Exception:log.exception('Account adapter error account=%s',ident)
        await asyncio.sleep(.5)

async def main():
    host.initialize();store.recover()
    # In-flight imports are intentionally retained; their existing per-item
    # checkpoints provide recovery. Web restarts no longer touch their state.
    jobs={}
    background=[asyncio.create_task(host.import_loop()),asyncio.create_task(host.dual_add.worker())]
    try:
        while True:
            for a in host.accounts():
                if a['id'] not in jobs:jobs[a['id']]=asyncio.create_task(account_loop(a['id']))
            await asyncio.to_thread(store.cleanup)
            await asyncio.sleep(10)
    finally:
        for task in [*jobs.values(),*background]:task.cancel()
        await asyncio.gather(*jobs.values(),*background,return_exceptions=True)

if __name__=='__main__':
    # Exactly one worker per control DB, including manually launched processes.
    import fcntl
    store.initialize()
    lock=open(str(store.path())+'.lock','a')
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
