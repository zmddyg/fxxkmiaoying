"""Portable, explicitly unencrypted session persistence for Miaoying."""

import argparse
import json
import os
import re
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


AUTH = 'https://service.hui51.cn/api/weixin'
NATIVE = 'https://www.aiphoto8.cn/api/graphql'
STATE_DIR = Path(os.environ.get('MIAOYING_STATE_DIR', str(Path(__file__).resolve().parent.parent / '运行状态'))).expanduser()
AUTH_MESSAGES = {'未登录', '请先登录', '请重新登录', '登录已过期', '登录状态已过期',
                 '登录已失效', 'unauthenticated', 'invalid token', 'jwt expired'}
AUTH_CODES = {'UNAUTHENTICATED', 'INVALID_TOKEN', 'TOKEN_EXPIRED'}


class Unavailable(Exception):
    """The service response did not establish whether the token is valid."""


class AccountMismatch(Exception):
    pass


class SessionBusy(Exception):
    pass


def load_state(directory):
    try:
        data = (directory / 'session.json').read_text(encoding='utf-8')
    except FileNotFoundError:
        return None
    state = json.loads(data)
    if (not isinstance(state, dict) or state.get('version') != 1
            or not isinstance(state.get('account'), str) or not state['account']
            or not isinstance(state.get('token'), str) or not state['token']):
        raise ValueError('invalid session')
    return state


def save_state(directory, token, account):
    state = {'version': 1, 'account': account, 'token': token}
    data = json.dumps(state, separators=(',', ':')).encode('utf-8')
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, path = tempfile.mkstemp(prefix='.session-', dir=directory)
    temporary = Path(path)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, directory / 'session.json')
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def session_lock(directory):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / 'session.lock').open('a+b') as handle:
        if handle.tell() == 0:
            handle.write(b'\0')
            handle.flush()
        handle.seek(0)
        if os.name == 'nt':
            import msvcrt
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as error:
                raise SessionBusy from error
        else:
            import fcntl
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise SessionBusy from error
        try:
            yield
        finally:
            if os.name == 'nt':
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request_json(url, method='GET', payload=None, token=None, timeout=8):
    headers = {'Accept': 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    if payload is not None:
        headers['Content-Type'] = 'application/json'
    data = json.dumps(payload).encode() if payload is not None else None
    request = Request(url, data=data, headers=headers, method=method)
    try:
        with build_opener(NoRedirect()).open(request, timeout=timeout) as response:
            http = response.status
            body = response.read(1_048_576)
    except HTTPError as error:
        error.close()
        return error.code, None
    except (OSError, URLError, ValueError) as error:
        raise Unavailable from error
    try:
        reply = json.loads(body)
    except (ValueError, UnicodeError):
        reply = None
    return http, reply if isinstance(reply, dict) else None


def authentication_rejected(http, reply):
    if http == 401:
        return True
    errors = reply.get('errors') if http == 200 and isinstance(reply, dict) else None
    if not isinstance(errors, list) or not errors:
        return False
    for error in errors:
        if not isinstance(error, dict):
            return False
        extension = error.get('extensions')
        code = extension.get('code') if isinstance(extension, dict) else None
        message = str(error.get('message', '')).strip(' 。！.!').lower()
        if (not isinstance(code, str) or code not in AUTH_CODES) and message not in AUTH_MESSAGES:
            return False
    return True


def account_for(token):
    http, reply = request_json(NATIVE, 'POST', {
        'query': 'query getMe { me { _id } }', 'operationName': 'getMe', 'variables': {}
    }, token=token)
    if authentication_rejected(http, reply):
        return None
    if http != 200 or reply is None:
        raise Unavailable
    if reply.get('errors'):
        raise Unavailable
    data = reply.get('data')
    if not isinstance(data, dict) or 'me' not in data:
        raise Unavailable
    identity = data['me']
    if identity is None:
        return None
    if not isinstance(identity, dict) or '_id' not in identity:
        raise Unavailable
    account = identity['_id']
    if account in (None, '', 0, '0'):
        return None
    if not isinstance(account, str):
        raise Unavailable
    return account


def authorize_qr():
    import queue
    import threading
    import tkinter as tk

    vendor = Path(__file__).with_name('vendor')
    if vendor.is_dir():
        sys.path.insert(0, str(vendor))
    import qrcode

    def issue():
        http, reply = request_json(AUTH + '/qrcodeLogin', 'POST', timeout=5)
        data = (reply or {}).get('data')
        if http != 200 or (reply or {}).get('statusCode') != 200 or not isinstance(data, dict):
            raise Unavailable
        url, scene = data.get('url'), data.get('sceneId')
        if not isinstance(url, str) or not isinstance(scene, (str, int)):
            raise Unavailable
        parsed = urlsplit(url)
        host = parsed.hostname or ''
        if parsed.scheme not in ('http', 'https') or (host != 'weixin.qq.com' and not host.endswith('.weixin.qq.com')):
            raise Unavailable
        return url, str(scene)

    def poll(scene):
        http, reply = request_json(AUTH + '/getUserWithSceneId/' + quote(scene, safe=''), timeout=4)
        if http != 200 or reply is None:
            raise Unavailable
        if reply.get('statusCode') != 200:
            return None
        data = reply.get('data')
        token = data.get('token') if isinstance(data, dict) else None
        if not isinstance(token, str) or not token:
            raise Unavailable
        return token

    messages = queue.SimpleQueue()
    stopped = threading.Event()
    result = None
    failure = False
    root = tk.Tk()
    root.title('秒应 · 保存本机授权')
    root.attributes('-topmost', True)
    label = tk.Label(root, text='准备好后，点击生成二维码，再用本人手机微信扫描。', padx=22, pady=12)
    label.pack()
    canvas = tk.Canvas(root, width=330, height=330, background='white', highlightthickness=0)
    canvas.pack(padx=10)
    controls = tk.Frame(root)
    controls.pack(pady=10)
    tk.Button(controls, text='取消', command=root.quit).pack(side='right', padx=5)
    root.protocol('WM_DELETE_WINDOW', root.quit)

    def worker():
        try:
            url, scene = issue()
            qr = qrcode.QRCode(border=4)
            qr.add_data(url)
            matrix = qr.get_matrix()
            end = time.monotonic() + 25
            messages.put(('qr', matrix))
            if stopped.wait(5):
                return
            while not stopped.is_set() and time.monotonic() < end:
                token = poll(scene)
                if token:
                    messages.put(('approved', token))
                    return
                stopped.wait(2)
            messages.put(('expired', None))
        except (Unavailable, ValueError, OSError):
            messages.put(('unavailable', None))

    def receive():
        nonlocal result, failure
        while not messages.empty():
            event, value = messages.get_nowait()
            if event == 'qr':
                matrix = value
                scale = 330 // len(matrix)
                offset = (330 - scale * len(matrix)) // 2
                for y, row in enumerate(matrix):
                    for x, pixel in enumerate(row):
                        if pixel:
                            left, top = offset + x * scale, offset + y * scale
                            canvas.create_rectangle(left, top, left + scale, top + scale,
                                                    fill='black', outline='')
                label.config(text='请用本人手机微信扫描，并确认登录')
            elif event == 'approved':
                result = value
                root.quit()
                return
            elif event in ('expired', 'unavailable'):
                canvas.delete('all')
                failure = event == 'unavailable'
                label.config(text='二维码已过期，可以重新生成。' if not failure
                             else '授权服务暂不可用，可以稍后重试。')
                generate.config(state='normal')
        root.after(90, receive)

    def start():
        generate.config(state='disabled')
        label.config(text='正在获取秒应登录二维码…')
        threading.Thread(target=worker, daemon=True).start()

    generate = tk.Button(controls, text='生成二维码', command=start)
    generate.pack(side='left', padx=5)
    root.after(90, receive)
    root.after(120_000, root.quit)
    try:
        root.mainloop()
    finally:
        stopped.set()
        root.destroy()
    if result is None and failure:
        raise Unavailable
    return result


def ensure_session(directory=STATE_DIR, interactive=False, force=False):
    """Return a validated bearer and account in memory, or a status for the caller."""
    with session_lock(directory):
        saved = load_state(directory)
        if saved and not force:
            account = account_for(saved['token'])
            if account is not None:
                if account != saved['account']:
                    raise AccountMismatch
                return 'reused', saved['token'], account
        if not interactive:
            return 'reauthorization_required', None, None
        token = authorize_qr()
        if token is None:
            return 'authorization_incomplete', None, None
        account = account_for(token)
        if account is None:
            return 'authorization_rejected', None, None
        if saved and saved['account'] != account:
            raise AccountMismatch
        save_state(directory, token, account)
        return 'recovered' if saved else 'saved', token, account


def main():
    parser = argparse.ArgumentParser(description='检查或重新授权秒应会话。仅查询当前身份。')
    parser.add_argument('action', choices=('check', 'recover', 'reauthorize'), nargs='?', default='check')
    parser.add_argument('--json', action='store_true', help='输出固定状态，供其他程序判断')
    args = parser.parse_args()
    try:
        state, _, _ = ensure_session(interactive=args.action != 'check', force=args.action == 'reauthorize')
    except AccountMismatch:
        state = 'account_mismatch'
    except SessionBusy:
        state = 'session_busy'
    except Unavailable:
        state = 'service_unavailable'
    except (OSError, ValueError, ImportError):
        state = 'local_store_error'
    texts = {
        'saved': '已验证账号并明文保存授权。',
        'reused': '已复用本机授权，无需扫码。',
        'recovered': '已用同一账号重新授权，并更新明文会话。',
        'reauthorization_required': '需要扫码授权。请运行「恢复授权」。',
        'authorization_incomplete': '未完成扫码授权。可以重新运行。',
        'authorization_rejected': '扫码后的会话未通过校验。',
        'account_mismatch': '本次账号与已保存的账号不一致，操作已停止。',
        'session_busy': '已有会话检查或授权窗口正在运行，请稍后重试。',
        'service_unavailable': '暂时无法确认会话，请稍后重试。',
        'local_store_error': '无法访问本地会话或授权组件。',
    }
    print(json.dumps({'status': state}) if args.json else texts[state])
    if state in ('saved', 'reused', 'recovered'):
        return 0
    if state in ('reauthorization_required', 'authorization_incomplete', 'authorization_rejected'):
        return 2
    return 3


if __name__ == '__main__':
    raise SystemExit(main())
