"""Interactive owner/token configuration; never print credentials."""
import getpass
import json
import os
from pathlib import Path

def main():
    os.umask(0o077)
    folder=Path(__file__).parent/'bot-data'
    path=folder/'config.json'
    if path.exists():
        raise SystemExit('Configuration already exists; nothing changed.')
    owner=input('Your numeric Telegram user ID: ').strip()
    if not owner.isascii() or not owner.isdigit() or int(owner)<=0:
        raise SystemExit('Use a positive numeric Telegram user ID; nothing saved.')
    token=getpass.getpass('Telegram bot token: ').strip()
    prefix,separator,secret=token.partition(':')
    if not separator or not prefix.isascii() or not prefix.isdigit() or not secret:
        raise SystemExit('Invalid token format; nothing saved.')
    folder.mkdir(mode=0o700,parents=True,exist_ok=True)
    (folder/'state').mkdir(mode=0o700,exist_ok=True)
    with path.open('x') as stream:
        json.dump({'token':token,'allowed_user_id':int(owner)},stream)
    path.chmod(0o600)
    print('Bot configuration saved privately.')

if __name__=='__main__':
    main()
