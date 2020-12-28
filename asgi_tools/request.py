"""ASGI Request."""

from cgi import parse_header, parse_multipart
from functools import wraps, cached_property
from http import cookies
from io import BytesIO
from json import loads
from urllib.parse import parse_qsl

from multidict import CIMultiDict, MultiDict
from yarl import URL

from . import ASGIDecodeError, DEFAULT_CHARSET


def process_decode(meta=None, message=None):
    """Handle errors."""

    def decorator(amethod):

        @wraps(amethod)
        async def wrapper(self, *args, **kwargs):
            try:
                if not meta:
                    return await amethod(self, *args, **kwargs)
                if meta not in self.meta:
                    self.meta[meta] = await amethod(self, *args, **kwargs)
                return self.meta[meta]
            except (LookupError, ValueError):
                raise ASGIDecodeError(message)

        return wrapper
    return decorator


class Request(dict):
    """Scope to request parser."""

    def __init__(self, scope, receive=None, send=None):
        """Create a request based on the given scope."""
        super(Request, self).__init__(scope)
        self.receive = receive
        self._body = None

    def __getattr__(self, name):
        """Proxy the request's unknown attributes to scope."""
        return self[name]

    @cached_property
    def meta(self):
        """Prepare a meta data for the request."""
        content_type, opts = parse_header(self.headers.get('content-type', ''))
        return {'opts': opts, 'content-type': content_type}

    @cached_property
    def url(self):
        """Get an URL."""
        host, port = self.get('server') or (None, None)
        host = self.headers.get('host') or host
        host, _, _ = host.partition(':')
        return URL.build(
            scheme=self.get('scheme', 'http'), host=host, port=port, encoded=True,
            path=self.get("root_path", "") + self["path"],
            query_string=self.get("query_string", b"").decode("latin-1"),
        )

    @cached_property
    def headers(self):
        """Parse headers from self scope."""
        return CIMultiDict(
            [[v.decode('latin-1') for v in item] for item in self.get('headers', [])])

    @cached_property
    def cookies(self):
        """Parse cookies from self scope."""
        data = {}
        for chunk in self.headers.get('cookie', '').split(';'):
            key, _, val = chunk.partition('=')
            data[key.strip()] = cookies._unquote(val.strip())

        return data

    @property
    def query(self):
        """Get a query part."""
        return self.url.query

    @property
    def charset(self):
        """Get a charset."""
        return self.meta['opts'].get('charset', DEFAULT_CHARSET)

    @property
    def content_type(self):
        """Get a content type."""
        return self.meta['content-type']

    async def stream(self):
        """Stream ASGI flow."""
        if not self.receive:
            raise RuntimeError('Request doesnt have a receive coroutine')

        message = await self.receive()
        yield message.get('body', b'')
        while message.get('more_body'):
            message = await self.receive()
            yield message.get('body', b'')

    async def body(self):
        """Read the request body."""
        if self._body is None:
            chunks = []
            async for chunk in self.stream():
                chunks.append(chunk)

            self._body = b"".join(chunks)

        return self._body

    @process_decode(message='Invalid Encoding')
    async def text(self):
        """Read the request text."""
        body = await self.body()
        charset = self.charset or DEFAULT_CHARSET
        return body.decode(charset)

    @process_decode(meta='json', message='Invalid JSON')
    async def json(self):
        """Read the request json."""
        text = await self.text()
        return loads(text)

    @process_decode(meta='form', message='Invalid Form Data')
    async def form(self):
        """Read the request formdata."""
        form = MultiDict()

        # TODO: Improve multipart parsing
        if self.content_type == 'multipart/form-data':
            pdict = dict(self.meta['opts'])
            pdict['boundary'] = bytes(pdict.get('boundary', ''), self.charset)
            pdict['CONTENT-LENGTH'] = self.headers.get('content-length')
            data = parse_multipart(BytesIO(await self.body()), pdict, encoding=self.charset)
            for name, values in data.items():
                for val in values:
                    form[name] = val

            return form

        data = await self.body()
        query = data.decode(self.charset)
        form.extend(parse_qsl(qs=query, keep_blank_values=True, encoding=self.charset))

        return form
