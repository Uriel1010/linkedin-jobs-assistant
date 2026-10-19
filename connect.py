"""Interactive real-browser sign-in over an SSH-forwarded noVNC desktop."""
import asyncio
import fcntl
import json
import os
import secrets
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import async_playwright
from storage import DATA


def wait_port(port):
    for _ in range(100):
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError('Desktop service did not start')


async def main():
    os.umask(0o077)
    DATA.mkdir(mode=0o700, exist_ok=True, parents=True)
    guard = (DATA / 'connect.guard').open('w')
    try:
        fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit('Another connection is already running.')
    processes = []
    marker = DATA / 'connecting.lock'
    marker.touch()
    try:
        with tempfile.TemporaryDirectory(prefix='linkedin-connect-') as temp:
            password = secrets.token_hex(4)  # VNC accepts eight characters.
            password_file = str(Path(temp) / 'vnc.pass')
            subprocess.run(['x11vnc', '-storepasswd', password, password_file],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            processes.append(subprocess.Popen(['Xvfb', ':99', '-screen', '0', '1280x900x24', '-nolisten', 'tcp'],
                                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            for _ in range(100):
                if Path('/tmp/.X11-unix/X99').exists():
                    break
                await asyncio.sleep(0.1)
            processes.append(subprocess.Popen(['x11vnc', '-display', ':99', '-rfbport', '5900',
                                               '-localhost', '-forever', '-shared', '-rfbauth', password_file],
                                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            await asyncio.to_thread(wait_port, 5900)
            processes.append(subprocess.Popen(['websockify', '--web=/usr/share/novnc', '6080', '127.0.0.1:5900'],
                                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            await asyncio.to_thread(wait_port, 6080)
            os.environ['DISPLAY'] = ':99'
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=False, args=['--window-size=1280,900'])
                try:
                    context = await browser.new_context(viewport={'width':1280, 'height':800})
                    page = await context.new_page()
                    await page.goto('https://www.linkedin.com/login', wait_until='domcontentloaded')
                    print('\nOpen http://127.0.0.1:6080/vnc.html in your local browser and click Connect.')
                    print('Temporary desktop password:', password)
                    print('Sign into LinkedIn in that browser and complete any verification yourself.')
                    print('Your password is entered directly into LinkedIn; only session state will be saved.')
                    while True:
                        answer = await asyncio.to_thread(input, '\nAfter login press Enter to save, or type cancel: ')
                        if answer.strip().lower() == 'cancel':
                            print('Cancelled; existing saved session kept.')
                            return
                        cookies = await context.cookies('https://www.linkedin.com')
                        path = urlsplit(page.url).path
                        if not any(c['name'] == 'li_at' and c['value'] for c in cookies) or any(s in path for s in ['/login', '/checkpoint', '/authwall', '/uas/']):
                            print('Login is not complete yet. Finish signing in, then try again.')
                            continue
                        state = await context.storage_state()
                        # Save only LinkedIn cookies, no other browsing state.
                        state = {'cookies':[c for c in state['cookies'] if c['domain'].lstrip('.') in ('linkedin.com', 'www.linkedin.com')], 'origins':[]}
                        tmp = DATA / 'session.json.tmp'
                        tmp.write_text(json.dumps(state))
                        tmp.chmod(0o600)
                        tmp.replace(DATA / 'session.json')
                        print('Session saved privately on the server. The collector will use it automatically.')
                        return
                finally:
                    await browser.close()
    finally:
        for proc in reversed(processes):
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        marker.unlink(missing_ok=True)
        guard.close()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, EOFError):
        print('\nConnection closed. No new session saved.')
