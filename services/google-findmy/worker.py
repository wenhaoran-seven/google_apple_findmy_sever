import contextlib
import json
import logging
import os
import sys
import backend


def location_pool(protocol):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from concurrent_locations import LocationSession, RateLimited, PushDisconnected
    from store import Store
    write_lock = threading.Lock()
    slots = threading.Semaphore(10)
    def emit(data):
        with write_lock:
            protocol.write(json.dumps(data, ensure_ascii=False)+'\n')
            protocol.flush()
    def error_result(error):
        return {'ok':False, 'error':str(error) if isinstance(error, backend.PublicError)
                else f'操作未完成（{type(error).__name__}）。请检查授权、网络与设备注册状态。',
                'rate_limited':isinstance(error, RateLimited),
                'reset_connection':isinstance(error, PushDisconnected)}
    store = Store()
    session = None
    try:
        backend.setup(store)
        session = LocationSession(store)
        session.start()
    except Exception as error:
        emit({'ready':False, **error_result(error)})
        return
    emit({'ready':True})
    def locate(message):
        try:
            result = {'ok':True, 'data':session.locate(message['device_id'])}
        except Exception as error:
            result = error_result(error)
        try:
            emit({'id':message['id'], **result})
        finally:
            slots.release()
    try:
        with ThreadPoolExecutor(max_workers=10) as executor:
            for line in sys.stdin:
                message = json.loads(line)
                if not all(isinstance(message.get(k), str) and 0<len(message[k])<100 for k in ('id','device_id')):
                    raise ValueError('Invalid IPC request')
                slots.acquire()
                executor.submit(locate, message)
    finally:
        session.close()


if __name__ == '__main__':
    logging.disable(logging.CRITICAL)
    if sys.argv[1] == 'locate-pool':
        protocol = sys.stdout
        with open(os.devnull, 'w') as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            location_pool(protocol)
        os._exit(0)
    with open(os.devnull, 'w') as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
        try:
            result = {'ok': True, 'data': backend.run(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)}
        except BaseException as error:
            # Only our explicit safe messages are exposed. Upstream errors may contain tokens.
            safe = isinstance(error, backend.PublicError)
            result = {'ok': False, 'error': str(error) if safe else f'操作未完成（{type(error).__name__}）。请检查授权、网络与设备注册状态。'}
    sys.stdout.write(json.dumps(result, ensure_ascii=False))
    sys.stdout.flush()
    # FCM uses daemon threads; no background loop survives this bounded job.
    os._exit(0)
