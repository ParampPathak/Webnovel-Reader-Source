"""Public-only HTTP fetches with DNS pinning, bounded bodies and redirect checks."""
import http.client
import ipaddress
import socket
import ssl
import time
import threading
import queue
from types import SimpleNamespace
from urllib.parse import urlsplit,urljoin,urlunsplit

MAX_PAGE_BYTES = 5*1024*1024
_dns_slots = threading.BoundedSemaphore(4)


def resolve_bounded(host, port, timeout=10):
    # libc DNS has no Python socket timeout. Limit both wait time and the
    # number of outstanding daemon lookups if a resolver becomes unresponsive.
    deadline=time.monotonic()+timeout
    if not _dns_slots.acquire(timeout=timeout):
        raise TimeoutError('Remote DNS lookup timed out. Retry when DNS is available.')
    result=queue.Queue(maxsize=1)
    def lookup():
        try:result.put((socket.getaddrinfo(host,port,type=socket.SOCK_STREAM),None))
        except Exception as e:result.put((None,e))
        finally:_dns_slots.release()
    threading.Thread(target=lookup,name='source-dns',daemon=True).start()
    try:addresses,error=result.get(timeout=max(.001,deadline-time.monotonic()))
    except queue.Empty as e:raise TimeoutError('Remote DNS lookup timed out. Retry when DNS is available.') from e
    if error:raise error
    return addresses


def public_url(url,resolve=True,timeout=10):
    if not isinstance(url,str) or len(url)>4096:
        raise ValueError('Invalid remote URL.')
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise ValueError('Invalid remote URL.') from exc
    if parsed.scheme not in {'http','https'} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('Use a public HTTP or HTTPS URL without credentials.')
    if port not in (None,80,443):
        raise ValueError('Only standard HTTP/HTTPS ports are supported.')
    host = parsed.hostname.rstrip('.').lower()
    if host == 'localhost' or host.endswith(('.localhost','.local','.internal')) or '%' in host:
        raise ValueError('Private or local network URLs are not allowed.')
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal and (not literal.is_global or literal.is_multicast):
        raise ValueError('Private or reserved network addresses are not allowed.')
    addresses = []
    if resolve:
        try:
            addresses = list(dict.fromkeys(row[4][0] for row in resolve_bounded(host,port or (443 if parsed.scheme=='https' else 80),timeout)))
        except socket.gaierror as exc:
            raise ValueError('Could not resolve the remote hostname.') from exc
        if not addresses or any((not ipaddress.ip_address(address).is_global or ipaddress.ip_address(address).is_multicast) for address in addresses):
            raise ValueError('Remote hostname resolves to a private or reserved network address.')
    return parsed,addresses


def fetch(url,headers=None,max_bytes=MAX_PAGE_BYTES,redirect_check=None):
    from discovery import remaining
    deadline=time.monotonic()+remaining()
    for _ in range(9):
        remaining=deadline-time.monotonic()
        if remaining<=0:raise TimeoutError('Remote page timed out.')
        parsed,addresses = public_url(url,timeout=min(10,remaining))
        port = parsed.port or (443 if parsed.scheme=='https' else 80)
        def pinned_connection(_address,timeout=None,source_address=None):
            last = None
            for address in addresses:
                try:
                    remaining=deadline-time.monotonic()
                    if remaining<=0: raise TimeoutError('Remote page timed out.')
                    return socket.create_connection((address,port),min(timeout or 25,remaining),source_address)
                except OSError as exc:
                    last=exc
            raise last or OSError('No remote address.')
        cls = http.client.HTTPSConnection if parsed.scheme=='https' else http.client.HTTPConnection
        options = {'context':ssl.create_default_context()} if parsed.scheme=='https' else {}
        conn = cls(parsed.hostname,port,timeout=min(25,max(.01,deadline-time.monotonic())),**options)
        # TLS still uses original hostname for certificate/SNI, while the socket
        # only reaches validated addresses. No environment proxies or second DNS.
        conn._create_connection = pinned_connection
        def expire():
            # An inactivity timeout alone does not stop a server dripping header
            # bytes forever. Interrupt the actual socket at the total deadline.
            sock=getattr(conn,'sock',None)
            if sock is not None:
                try: sock.shutdown(socket.SHUT_RDWR)
                except OSError: pass
            conn.close()
        watchdog=threading.Timer(max(.01,deadline-time.monotonic()),expire);watchdog.daemon=True;watchdog.start()
        try:
            path = urlunsplit(('','',parsed.path or '/',parsed.query,''))
            conn.request('GET',path,headers={**(headers or {}),'Accept-Encoding':'identity'})
            response = conn.getresponse()
            if response.status in {301,302,303,307,308}:
                location = response.getheader('Location')
                if not location:
                    raise ValueError('Redirect has no destination.')
                url = urljoin(url,location)
                public_url(url,resolve=False)
                if redirect_check: redirect_check(url)
                continue
            if response.status != 200:
                raise ValueError(f'Source returned HTTP {response.status}; authentication or site restrictions are not bypassed.')
            size = response.getheader('Content-Length')
            if size and int(size)>max_bytes:
                raise ValueError('Remote page exceeds the safe size budget.')
            pieces=[];length=0
            while True:
                if time.monotonic()>deadline: raise ValueError('Remote page timed out.')
                block=getattr(response,'read1',response.read)(min(64*1024,max_bytes-length+1))
                if not block: break
                length+=len(block)
                if length>max_bytes: raise ValueError('Remote page exceeds the safe size budget.')
                pieces.append(block)
            raw=b''.join(pieces)
            encoding = response.headers.get_content_charset() or 'utf-8'
            try:
                text = raw.decode(encoding,errors='replace')
            except LookupError:
                text = raw.decode('utf-8',errors='replace')
            return SimpleNamespace(text=text,content=raw,headers=response.headers,url=url)
        finally:
            watchdog.cancel()
            conn.close()
            watchdog.join(timeout=.1)
    raise ValueError('Too many redirects.')
