import random
import os
import time
from threading import Event
from utils import BaseProcess, download_if_modified

import requests
from singbox2proxy import SingBoxProxy
from concurrent.futures import ThreadPoolExecutor, as_completed
import subprocess
import json
from datetime import datetime

from http.server import SimpleHTTPRequestHandler
import socketserver

debug = False

class ProxyTestResult:
    url: str
    is_ok: bool
    error: Exception
    quality: float
    def __repr__(self)->str:
        return self.url

def check_speed(p: SingBoxProxy):
    started = time.time()
    response = p.request("GET", "http://ipv4.download.thinkbroadband.com/5MB.zip")
    if response.status_code != 200:
        return None
    else:
        t = time.time() - started
        if t == 0:
            raise Exception("t == 0")
        else:
            return 5.0 / t

def check_proxy(url:str) -> ProxyTestResult:
    res = ProxyTestResult()
    res.url = url
    res.is_ok = False
    try:
        p = SingBoxProxy(url)
        response = p.request("GET", "https://api.ipify.org?format=json", timeout=(5, 5))
        if response.status_code == 200:
            res.quality = check_speed(p)
            res.is_ok = res.quality is not None
    except Exception as e:
        res.error = e
    return res

def kill_signbox_processes():
    os.system('taskkill /f /im sing-box.exe')

def check_proxies(proxy_urls:[str], max_workers:int=20):
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(check_proxy, url) for url in proxy_urls]
        for res in as_completed(futures):
            yield res.result()

def dt2str(dt):
    return str(dt) if dt else None

class WhitelistChecker(BaseProcess):

    def __init__(self):
        super().__init__()
        self.is_whitelists = False
        self.is_down = True
        self._next = None

    def _check_url(self, url:str)->bool:
        try:
            r = requests.get("https://ya.ru")
            return r.status_code == 200
        except Exception:
            return False

    def set_down(self, is_down:bool):
        if self.is_down == is_down:
            return
        self.is_down = is_down
        self.notify_listeners()

    def set_whitelists(self, is_whitelists:bool):
        if self.is_whitelists == is_whitelists:
            return
        self.is_whitelists = is_whitelists
        self.notify_listeners()

    def _process(self):
        if self.reached(self._next):
            self.set_down(not self._check_url("https://ya.ru") and not self._check_url("https://ozon.ru"))
            if not self.is_down:
                self.set_whitelists(not self._check_url("https://google.com") and not self._check_url("https://profinance.ru"))
            self._next = self.schedule_delay(60)

    def _on_error(self, e:Exception):
        self.set_down(True)
        super()._on_error(e)

    def get_status(self)->dict:
        return {
            "down": self.is_down,
            "whitelists": self.is_whitelists,
            "last_error": str(self.get_last_error()),
        }

class ProxyChecker(BaseProcess):

    def __init__(self, wl_checker:WhitelistChecker=None):
        super().__init__()
        self.check_results:[ProxyTestResult] = []
        self.check_results_version = 0
        self.wl_checker = wl_checker
        self._last_whitelist = False
        if wl_checker:
            self.subscribe(wl_checker)
        self._next_check = None
        self.sleep_on_error = 300
        self._last_result_at = None
        self._proxy_count = None
        self._proxy_selected = None
        self._completion_percent = None

    def _check(self):
        file_name = 'WHITE-CIDR-RU-all.txt'
        try:
            self._fetch_proxies(file_name)
        except Exception as e:
            print(str(e))
            pass

        proxies = self._load_proxies(file_name)
        if debug:
            proxies = proxies[:50]
        self._proxy_count = len(proxies)
        if self._proxy_count == 0:
            self._completion_percent = 100
        else:
            self._completion_percent = 0
            check_results = []
            self._proxy_selected = 0
            proxy_checked = 0
            for r in check_proxies(proxies):
                proxy_checked += 1
                self._completion_percent = int(100 * proxy_checked / self._proxy_count)
                if r.is_ok:
                    self._proxy_selected += 1
                    check_results.append(r)

        if len(check_results) != 0:
            check_results = sorted(check_results, key=lambda r: r.quality, reverse=True)

        self._last_result_at = datetime.now()
        self.check_results = check_results
        self.check_results_version += 1
        self.notify_listeners()

        #with open('selected-proxies.txt') as f:

    def check(self):
        self._next_check = None
        self.signal()

    def _process(self):

        is_whitelists = False
        if wl_checker:
            is_whitelists = wl_checker.is_whitelists
            if self._last_whitelist != is_whitelists:
                self._next_check = None
            if self.wl_checker.is_down:
                return

        if self.reached(self._next_check):
            self._check()
            self._last_whitelist = is_whitelists
            self._next_check = self.schedule_delay(3600 if not debug else 60)

    def _fetch_proxies(self, file_name:str):
        file_name = 'WHITE-CIDR-RU-all.txt'
        return download_if_modified(f'https://raw.githubusercontent.com/igareck/vpn-configs-for-russia/refs/heads/main/{file_name}', file_name)

    def _load_proxies(self, file_name:str)->[str]:
        with open(file_name, 'r') as f:
            proxies = []
            for line in f:
                line = line.strip()
                if len(line) == 0:
                    continue
                if line.startswith('#'):
                    continue
                proxies.append(line)
            return proxies

    def get_status(self)->dict:
        check_results = self.check_results
        return {
            "loaded": self._proxy_count,
            "checked_percent": self._completion_percent,
            "selected": self._proxy_selected,
            "output": {
                "count": len(self.check_results),
                "best_quality": check_results[0].quality if len(check_results) != 0 else None,
                "best_url": check_results[0].url if len(check_results) != 0 else None,
                "last_result": dt2str(self._last_result_at),
            },
            "last_error": str(self.get_last_error()),
        }

class ProxyProcessController(BaseProcess):

    def __init__(self, checker: ProxyChecker):
        super().__init__()
        self.checker = checker
        if checker:
            self.subscribe(checker)
        self.sleep_on_error = 5
        self._check_results_version = 0
        self._proxy:str = None
        self._used_proxy:str = None
        self.__process__ = None
        self._exit_code = None
        self._config_dir = "."
        self._singbox = SingBoxProxy(None)
        self._process_started = None
        self._process_stopped = None

    def _check_process(self)->bool:
        if not self.__process__:
            return False
        self._exit_code = self.__process__.poll()
        if self._exit_code is not None:
            self.__process__ = None
            self._process_stopped = datetime.now()
            return False
        return True

    def _stop_process(self)->bool:
        if not self.__process__:
            return True
        self.__process__.terminate()

    def _make_config(self, proxy_url:str):

        self._singbox.socks_port = 2080
        self._singbox.http_port = None
        self._singbox.config_url = proxy_url
        config = self._singbox.generate_config()

        file_name = os.path.join(self._config_dir, "signbox_config.json")
        with open(file_name, 'w') as f:
            json.dump(config, f)
        return file_name

    def _update_proxy(self):
        plist:[ProxyTestResult] = self.checker.check_results
        if self._proxy:
            i = 0
            while i < len(plist) and plist[i].url != self._proxy:
                i += 1
            if i < len(plist):
                if plist[i].quality * 5 >=  plist[0].quality:
                    return
        if len(plist) == 0:
            #self.set_proxy(None)
            return
        i = random.randint(0, len(plist)) if debug else 0
        self.set_proxy(plist[i].url)

    def _process(self):

        if self.checker:
            v = self.checker.check_results_version
            if v != self._check_results_version:
                self._update_proxy()
                self._check_results_version = v

        has_process = self._check_process()
        if has_process:
            if self._proxy != self._used_proxy:
                self.__process__.terminate()
                self.schedule_delay(3)
            else:
                self.schedule_delay(10)
        else:
            self._used_proxy = self._proxy
            #subprocess.Popen(commands, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd = cwd, env = env)
            config_file = self._make_config(self._used_proxy)
            command = ['sing-box', 'run', '-c', config_file, '-D', self._config_dir]
            self.__process__ = subprocess.Popen(command, stderr=subprocess.STDOUT)
            self._process_started = datetime.now()
            self.schedule_delay(3)

    def set_proxy(self, proxy:str):
        if proxy == self._proxy:
            return
        self._proxy = proxy
        self.signal()

    def get_status(self)->dict:
        return {
            "has_process": self.__process__ is not None,
            "proxy": self._used_proxy,
            "process_started": dt2str(self._process_started),
            "process_stopped": dt2str(self._process_stopped),
            "last_error": str(self.get_last_error()),
        }


if __name__ == '__main__':

    kill_signbox_processes()

    wl_checker = WhitelistChecker()
    proxy_checker = ProxyChecker(wl_checker)
    proxy_controller = ProxyProcessController(proxy_checker)

    proxy_checker.start()
    wl_checker.start()
    proxy_controller.start()

    class HTTPServer(SimpleHTTPRequestHandler):

        def do_GET(self):
            if self.path == '/' or self.path == '/status':
                res = {
                    "InternetChecker": wl_checker.get_status(),
                    "ProxyChecker": proxy_checker.get_status(),
                    "SingboxController": proxy_controller.get_status(),
                }
                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                data = json.dumps(res, ensure_ascii=True, indent=4).encode()
                print(data)
                self.wfile.write(data)
            elif self.path == '/proxies':
                proxies = proxy_checker.check_results
                self.send_response(200)
                self.send_header('Content-type', 'text/plain')
                self.end_headers()
                self.wfile.write(('\n'.join([p.url for p in proxies])).encode())
            elif self.path == '/check':
                proxy_checker.check()
                self.send_response(200)
                self.send_header('Content-type', 'text/plain')
                self.end_headers()
                self.wfile.write(b"Ok")

    with socketserver.TCPServer(("", 2081), HTTPServer) as httpd:
        httpd.serve_forever()

