"""A loopback-only HTTP application using the Python standard library."""
import argparse
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import threading
import time
import traceback
from urllib.parse import quote, urlsplit

from .added_images import with_added_images, MAX_PROJECT
from .edits import apply_edits
from .align import align
from .books import BookError, MAX_FILE, parse_donor, parse_text
from .demo import demo_files, mojimoon_demo_files
from .export import build_epub, build_report, initial_settings
from .wenku import GeneratorJobs
from .local_txt import convert_txt

WEB = Path(__file__).resolve().parent.parent / 'web'
MAX_REQUEST = 2 * MAX_FILE + 1024 * 1024
SESSION_TTL = 4 * 60 * 60


class State:
    def __init__(self):
        self.sessions = {}
        self.lock = threading.Lock()
        self.busy = threading.Lock()
        self.generator = GeneratorJobs()

    def add(self, book, donor):
        token = secrets.token_urlsafe(24)
        with self.lock:
            now = time.monotonic()
            self.sessions = {k: v for k, v in self.sessions.items() if now - v[2] < SESSION_TTL}
            while len(self.sessions) >= 4:
                del self.sessions[min(self.sessions, key=lambda k: self.sessions[k][2])]
            self.sessions[token] = (book, donor, now)
        return token

    def get(self, token):
        with self.lock:
            value = self.sessions.get(token)
            if not value or time.monotonic() - value[2] > SESSION_TTL:
                self.sessions.pop(token, None)
                raise BookError('本次导入已过期，请重新导入文件并载入保存的校对项目。')
            self.sessions[token] = (value[0], value[1], time.monotonic())
            return value[:2]


class Handler(BaseHTTPRequestHandler):
    server_version = 'LocalEpubMerge/0.1'

    def log_message(self, fmt, *args):
        # Do not print filenames or document contents into the server log.
        if args and isinstance(args[0], str):
            print('[local] ' + args[0].split('?')[0], flush=True)

    def send(self, status, content, content_type, filename=None):
        if isinstance(content, str):
            content = content.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(content)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        if filename:
            self.send_header('Content-Disposition', "attachment; filename*=UTF-8''" + quote(filename, safe=''))
        self.end_headers()
        self.wfile.write(content)

    def json(self, status, data):
        self.send(status, json.dumps(data, ensure_ascii=False), 'application/json; charset=utf-8')

    def local_request(self, write=False):
        port = self.server.server_address[1]
        allowed = {f'127.0.0.1:{port}', f'localhost:{port}'}
        if self.headers.get('Host') not in allowed:
            self.json(403, {'error': '仅允许从本机地址访问。'})
            return False
        origin = self.headers.get('Origin')
        if write and origin and origin not in {'http://' + host for host in allowed}:
            self.json(403, {'error': '请从本地工具页面发起操作。'})
            return False
        return True

    def do_GET(self):
        if not self.local_request():
            return
        path = urlsplit(self.path).path
        try:
            static = {'/': ('index.html', 'text/html; charset=utf-8'),
                      '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                      '/style.css': ('style.css', 'text/css; charset=utf-8')}
            if path in static:
                name, media = static[path]
                self.send(200, (WEB / name).read_bytes(), media)
            elif path == '/api/health':
                self.json(200, {'ok': True, 'version': '0.10.0'})
            elif path.startswith('/api/wenku/jobs/'):
                parts = path.split('/')
                if len(parts) == 6 and parts[5] in {'epub','txt'}:
                    data, filename = self.server.state.generator.download(parts[4])
                    if not filename.lower().endswith('.'+parts[5]): raise BookError('下载格式与任务不一致。')
                    self.send(200, data, 'application/epub+zip' if parts[5]=='epub' else 'application/octet-stream', filename)
                elif len(parts) == 5:
                    self.json(200, self.server.state.generator.get(parts[4]))
                else:
                    raise BookError('无效的生成任务地址。')
            elif path.startswith('/api/image/'):
                parts = path.split('/')
                if len(parts) != 5:
                    raise BookError('无效图片地址。')
                book, donor = self.server.state.get(parts[3])
                image = next((x for x in donor.illustrations if x.id == parts[4]), None)
                if not image:
                    raise BookError('插图不存在。')
                self.send(200, image.data, image.media_type)
            else:
                self.json(404, {'error': '页面不存在。'})
        except BookError as exc:
            self.json(400, {'error': str(exc)})
        except (BrokenPipeError, ConnectionResetError):
            pass

    def body(self, limit=MAX_REQUEST):
        if self.headers.get('Transfer-Encoding'):
            raise BookError('不支持分块上传。')
        try:
            size = int(self.headers.get('Content-Length', '0'))
        except ValueError as exc:
            raise BookError('上传长度无效。') from exc
        if size <= 0 or size > limit:
            raise BookError('上传内容为空或超过大小限制（单个文件 80 MB）。')
        data = self.rfile.read(size)
        if len(data) != size:
            raise BookError('上传中断，请重新选择文件。')
        return data

    def import_files(self, required=('text','donor')):
        content_type = self.headers.get('Content-Type', '')
        if not content_type.startswith('multipart/form-data;'):
            raise BookError('请选择两份输入文件。')
        message = BytesParser(policy=policy.default).parsebytes(
            ('Content-Type: ' + content_type + '\r\nMIME-Version: 1.0\r\n\r\n').encode() + self.body())
        if not message.is_multipart():
            raise BookError('上传格式无效。')
        files = {}
        self.upload_settings = {}
        for part in message.iter_parts():
            key = part.get_param('name', header='content-disposition')
            if key == 'settings':
                settings = part.get_payload(decode=True)
                if len(settings)>64000: raise BookError('书籍信息过长。')
                self.upload_settings = json.loads(settings)
                continue
            if key not in required or key in files:
                continue
            filename = (part.get_filename() or '').replace('\\', '/').split('/')[-1]
            data = part.get_payload(decode=True)
            if not filename or not data or len(data) > MAX_FILE:
                raise BookError('文件为空或超过 80 MB。')
            files[key] = (data, filename)
        if set(files) != set(required):
            raise BookError('请选择 TXT 文件。' if len(required)==1 else '请同时选择中文正文和插图源。')
        return tuple(files[key] for key in required)

    def do_POST(self):
        if not self.local_request(write=True):
            return
        path = urlsplit(self.path).path
        try:
            if path == '/api/wenku/build':
                payload = json.loads(self.body(limit=4096))
                if not isinstance(payload, dict):
                    raise BookError('生成参数无效。')
                job = self.server.state.generator.start(payload.get('aid'), payload.get('volume', 1), payload.get('scraper', 'requests'), **({'vid':payload['vid']} if payload.get('vid') not in (None,'') else {}), **({'output_format':payload['format']} if 'format' in payload else {}))
                self.json(202, {'job': job})
            elif path == '/api/txt/build':
                text_input, = self.import_files(('text',))
                data, filename, _ = convert_txt(*text_input, self.upload_settings)
                self.send(200, data, 'application/epub+zip', filename)
            elif path in {'/api/analyze', '/api/demo'}:
                if not self.server.state.busy.acquire(blocking=False):
                    self.json(409, {'error': '正在分析另一组文件，请稍后再试。'})
                    return
                try:
                    if path == '/api/demo':
                        text, donor = mojimoon_demo_files()
                        text_input, donor_input = (text, '雨夜来信·mojimoon无图底稿.epub'), (donor, '雨夜来信·插图源.epub')
                    else:
                        text_input, donor_input = self.import_files()
                    book = parse_text(*text_input)
                    donor_book = parse_donor(*donor_input)
                    align(book, donor_book)
                    session = self.server.state.add(book, donor_book)
                    self.json(200, {'session': session, 'text': book.public(), 'donor': donor_book.public(),
                                    'settings': initial_settings(book, donor_book)})
                finally:
                    self.server.state.busy.release()
            elif path == '/api/images/preview':
                payload = json.loads(self.body(limit=MAX_PROJECT))
                if not isinstance(payload, dict):
                    raise BookError('新增图片请求无效。')
                _, donor = self.server.state.get(payload.get('session'))
                combined = with_added_images(donor, {'added_images': payload.get('added_images', [])})
                self.json(200, combined.public())
            elif path == '/api/text/edit':
                payload = json.loads(self.body(limit=8 * 1024 * 1024))
                if not isinstance(payload, dict):
                    raise BookError('段落修改请求无效。')
                book, _ = self.server.state.get(payload.get('session'))
                edited = apply_edits(book, {'text_edits': payload.get('text_edits', [])}, preserve_source=False)
                self.json(200, edited.public())
            elif path in {'/api/export', '/api/report'}:
                payload = json.loads(self.body(limit=MAX_PROJECT))
                if not isinstance(payload, dict):
                    raise BookError('请求格式无效。')
                book, donor = self.server.state.get(payload.get('session'))
                settings = payload.get('settings')
                if path == '/api/export':
                    result, media, ext = build_epub(book, donor, settings), 'application/epub+zip', '.epub'
                else:
                    result, media, ext = build_report(book, donor, settings), 'text/html; charset=utf-8', '·合并记录.html'
                from .metadata import normalize_volumes
                name = re_filename(normalize_volumes(str(settings.get('title', book.title))))
                self.send(200, result, media, name + ext)
            else:
                self.json(404, {'error': '接口不存在。'})
        except (BookError, json.JSONDecodeError, UnicodeError) as exc:
            self.json(400, {'error': str(exc)})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            traceback.print_exc()
            self.json(500, {'error': '处理失败；请查看运行窗口的错误信息，并尝试每卷一个文件。'})


def re_filename(name):
    import re
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name).strip(' .')[:100] or '合并电子书'


def make_server(port=8765):
    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    server.daemon_threads = True
    server.state = State()
    return server


def main():
    parser = argparse.ArgumentParser(description='先用 mojimoon 生成无图 EPUB，再与 auto-novel 匹配插图的本地工具。')
    parser.add_argument('--port', type=int, default=8765, help='本机端口，默认 8765')
    args = parser.parse_args()
    try:
        server = make_server(args.port)
    except OSError as exc:
        parser.exit(1, '无法启动本地服务：%s\n可尝试 --port 8766。\n' % exc)
    print('卷间 EPUB 合并原型已启动\n请在浏览器打开 http://127.0.0.1:%d\n按 Ctrl+C 停止。正文生成与缓存保存在 out/，导入文件在内存中合并。' % server.server_address[1], flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
