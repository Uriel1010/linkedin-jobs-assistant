"""Single public-feed worker. This process has no LinkedIn/browser/email credentials."""
import time
import storage
from source_store import heartbeat, claim, save_snapshot, fail
from source_providers import collect_source, SourceError


def tick():
    heartbeat()
    source=claim()
    if source is None: return False
    try:
        jobs,_=collect_source(source)
        added=save_snapshot(source,jobs)
        with storage.db() as con:
            con.execute("UPDATE source_runs SET status='ok',finished=?,jobs_count=?,added=? WHERE id=?",
                        (time.time(),len(jobs),added,source['run_id']))
    except SourceError as exc:
        fail(source,exc)
    except Exception:
        # Provider content or error objects must never be emitted to logs.
        fail(source,SourceError('unexpected_provider_response'))
    heartbeat()
    return True


def main():
    storage.init()
    while True:
        try:
            worked=tick()
        except Exception:
            worked=False
            print('Feed worker database unavailable; retrying later.',flush=True)
        time.sleep(5 if worked else 30)


if __name__=='__main__':
    main()
