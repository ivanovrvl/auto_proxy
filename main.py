import random
import os
import time
from threading import Event
from utils import BaseProcess, download_if_modified
import pickle

import requests
from singbox2proxy import SingBoxProxy
from concurrent.futures import ThreadPoolExecutor, as_completed
import subprocess
import json
from datetime import datetime

from http.server import SimpleHTTPRequestHandler
import socketserver
from collections import deque

debug = False
persist_checklist = True

socks_proxy_port = 2080
proxy = f"socks5://127.0.0.1:{socks_proxy_port}"
proxies = {"http":proxy, "https":proxy}

class TimeoutController(BaseProcess):

    def __init__(self, timeout:float):
        super().__init__()
        self._queue = deque()
        self.timeout = timeout
        self.sleep_on_error = 5
        self._current = None

    def put(self, callback):
        was_empty = False if self._queue else True
        self._queue.append((time.time() + self.timeout, callback))
        if was_empty:
            self.signal()

    def _process(self):
        try:
            while True:
                if self._current is None:
                    self._current = self._queue.pop()
                    if self._current is None:
                        break
                if not self.reached(self._current[0]):
                    break
                try:
                    self._current[1]()
                except Exception:
                    pass
                self._current = None
        except IndexError:
            pass

    def _on_error(self, e:Exception):
        super()._on_error(e)

timeout_controller: TimeoutController = None

class ProxyTestResult:
    url: str
    is_ok: bool
    error: Exception
    quality: float

    def __repr__(self)->str:
        return self.url

def check_speed(p: SingBoxProxy):
    started = time.time()
    response = p.request("GET", "http://ipv4.download.thinkbroadband.com/5MB.zip", timeout=30)
    if response.status_code != 200:
        return None
    else:
        t = time.time() - started
        if t == 0:
            raise Exception("t == 0")
        else:
            return 5.0 / t

def test_proxy(url:str) -> ProxyTestResult:
    res = ProxyTestResult()
    res.url = url
    res.is_ok = False
    try:
        tc = timeout_controller
        with SingBoxProxy(url) as p:
            #p.client.auto_retry = False
            if tc:
                def terminate():
                    try:
                        p.client.close()
                    except:
                        pass
                    p.client = None
                    if p.singbox_process:
                        p.singbox_process.terminate()
                tc.put(terminate)
            response = p.request("GET", "https://api.ipify.org?format=json", timeout=10)
            if response.status_code == 200:
                res.quality = check_speed(p)
                res.is_ok = res.quality is not None
    except Exception as e:
        res.error = e
    return res

def kill_signbox_processes():
    os.system('taskkill /f /im sing-box.exe')

def test_proxies(proxy_urls:list[str], max_workers:int=20):
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(test_proxy, url) for url in proxy_urls]
        for res in as_completed(futures):
            yield res.result()

def check_proxy(url:str)->int:
    try:
        with SingBoxProxy(url) as p:
            response = p.request("GET", "https://api.ipify.org?format=json", timeout=10)
            return response.status_code
    except Exception as e:
        print(str(e))
        return -1

def check_proxies(proxy_urls:list[str], max_workers:int=20)->list[int]:
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        return executor.map(check_proxy, proxy_urls)

def dt2str(dt):
    return str(dt) if dt else None

class InternetChecker(BaseProcess):

    def __init__(self):
        super().__init__()
        self.is_whitelists = False
        self.is_down = True
        self._next = None
        self.restored_count=0

    def _check_urls(self, urls:list[str])->bool:
        try:
            for url in urls:
                r = requests.get(url)
                return True
        except Exception:
            pass
        return False

    def set_down(self, is_down:bool):
        if self.is_down == is_down:
            return
        self.is_down = is_down
        if not is_down:
            self.restored_count += 1
        self.notify_listeners()

    def set_whitelists(self, is_whitelists:bool):
        if self.is_whitelists == is_whitelists:
            return
        self.is_whitelists = is_whitelists
        self.notify_listeners()

    def _process(self):
        if self.reached(self._next):
            self.set_down(not self._check_urls(["https://ya.ru", "https://lenta.ru"]))
            if not self.is_down:
                t1 = self._check_urls(["https://google.com"])
                t2 = self._check_urls(["https://profinance.ru"])
                if t1 and t2:
                    self.set_whitelists(False)
                elif not (t1 or t2):
                    self.set_whitelists(True)
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

class ProxyListLoader(BaseProcess):

    def __init__(self):
        super().__init__()
        self.proxy_list:list[str]=None
        self.proxy_list_version = 0
        self._next=None

    def _fetch_proxies_internal(self, file_name:str, proxies=None):
        tmp_file_name = "1.tmp"
        if download_if_modified(
            f'https://raw.githubusercontent.com/igareck/vpn-configs-for-russia/refs/heads/main/{file_name}',
            tmp_file_name,
            metafile_name=f"{file_name}.meta",
            timeout=15,
            proxies=proxies
        ):
            os.replace(tmp_file_name, file_name)
            return True
        return False

    def _fetch_proxies(self, file_name:str):
        try:            
            return self._fetch_proxies_internal(file_name)
        except Exception as e:
            print(str(e))
            return self._fetch_proxies_internal(file_name, proxies=proxies)
            
    def _load_proxies(self, file_name:str)->list[str]:
        with open(file_name, 'r', encoding="utf-8") as f:
            proxies = []
            for line in f:
                line = line.strip()
                if len(line) == 0:
                    continue
                if line.startswith('#'):
                    continue
                proxies.append(line)
        self.proxy_list=proxies
        self.proxy_list_version += 1
        self.notify_listeners()

    def _process(self):

        if self.reached(self._next):

            file_name = 'WHITE-CIDR-RU-all.txt'
            try:
                if self._fetch_proxies(file_name):
                    self._load_proxies(file_name)
                self._next = self.schedule_delay(15*60)
            except Exception as e:
                print(str(e))
                self._next = self.schedule_delay(20)

            if self.proxy_list is None:
                try:
                    self._load_proxies(file_name)
                except Exception as e:
                    print(str(e))
                    self._next = self.schedule_delay(60)

    def load(self):
        self._next=None
        self.signal()

    def get_status(self)->dict:
        return None

class ProxyListChecker(BaseProcess):

    def __init__(self, proxy_loader:ProxyListLoader, internet_checker:InternetChecker):
        super().__init__()
        self.proxy_loader = proxy_loader
        self.subscribe(proxy_loader)
        self.last_proxy_version=0

        self.internet_checker = internet_checker
        self.subscribe(internet_checker)
        self._last_whitelist = False

        self.check_results:list[ProxyTestResult] = []
        self.check_results_version = 0        
        self._next_check = None
        self.sleep_on_error = 300
        self._last_result_at = None
        self._proxy_count = None
        self._proxy_selected = None
        self._completion_percent = None
        self._check_started = None
        self._check_completed = None
        self.__loaded__=False
        self.__internet_restored__=0

    def get_status(self)->dict:
        check_results = self.check_results
        return {
            "loaded": self._proxy_count,
            "checked_percent": self._completion_percent,
            "selected": self._proxy_selected,
            "output": {
                "count": len(check_results),
                "best_quality": check_results[0].quality if len(check_results) != 0 else None,
                "best_url": check_results[0].url if len(check_results) != 0 else None,
                "last_result": dt2str(self._last_result_at),
            },
            "check_started": dt2str(self._check_started),
            "check_completed": dt2str(self._check_completed),
            "last_error": str(self.get_last_error()),
        }        

    def _check(self, proxy_list:list[str])->list[ProxyTestResult]:
        if self.internet_checker.is_down:
            return None
        last_restored_count = self.internet_checker.restored_count
        check_results = []
        self._proxy_selected = 0
        proxy_checked = 0
#        if debug:
#            proxy_list = proxy_list[:100]
        for r in test_proxies(proxy_list, max_workers=20):
            if self.internet_checker.is_down or last_restored_count != self.internet_checker.restored_count:
                return None
            proxy_checked += 1
            self._completion_percent = int(100 * proxy_checked / self._proxy_count)
            if r.is_ok:
                self._proxy_selected += 1
                check_results.append(r)
        if self.internet_checker.is_down or last_restored_count != self.internet_checker.restored_count:
            return None
        return sorted(check_results, key=lambda r: r.quality, reverse=True)

    def _process_check(self):
        proxy_list = self.proxy_loader.proxy_list
        self._check_started = datetime.now()
        if proxy_list is None or len(proxy_list)==0:
            check_results = []
            self._completion_percent = 100
            self._proxy_count = 0
            self._check_completed = datetime.now()
            return True
        else:
            self._proxy_count = len(proxy_list)
            check_results = self._check(proxy_list)
            if check_results is None:
                return False
            self._last_result_at = datetime.now()
            self.check_results = check_results
            return True

    def check(self):
        self._next_check = None
        self.signal()

    def __save__(self):
        with open('check_results.pkl', 'wb') as f:
            pickle.dump(self.check_results, f)

    def __load__(self):
        try:
            with open('check_results.pkl', 'rb') as f:
                loaded = pickle.load(f)            
            if loaded and len(loaded) != 0:
                self.check_results = loaded
                self.check_results_version += 1
                self.notify_listeners()
        except:
            pass

    def _process(self):

        if persist_checklist and not self.__loaded__:
            self.__load__()
            self.__loaded__ = True

        whitelists = self.internet_checker.is_whitelists
        internet_restored = self.internet_checker.restored_count
        if self._last_whitelist != whitelists or self.__internet_restored__ != internet_restored:
            self._next_check = None

        proxy_list_version = self.proxy_loader.proxy_list_version
        if self.last_proxy_version != proxy_list_version:
            self._next_check = None

        if not self.internet_checker.is_down and self.reached(self._next_check):
            if self._process_check():
                self.last_proxy_version = proxy_list_version
                self._last_whitelist = whitelists
                self.__internet_restored__ = internet_restored
                self.check_results_version += 1
                self.notify_listeners()
                if persist_checklist:
                    self.__save__()
                self._next_check = self.schedule_delay(3600 if not debug else 60)

class ProxyInfo:

    def __init__(self, proxy:ProxyTestResult):
        self.proxy = proxy
        self.fail=0
        self.success=0
        self.last_result:bool=False
        self.result_seq=0
        self.next_check=None

    def url(self):
        return self.proxy.url

    def set_check_result(self, success:bool):
        if success:
            self.success += 1
        else:
            self.fail += 1
        if self.last_result != success:
            self.result_seq = 1
            self.last_result = success
        else:
            self.result_seq += 1

    def is_bad(self)->bool:
        return not self.last_result and self.result_seq >= 2
    
    def is_good(self)->bool:
        return self.last_result and self.result_seq > 0

class ProxySelector(BaseProcess):

    def __init__(self, pl_checker:ProxyListChecker, internet_checker:InternetChecker=None):
        super().__init__()
        self.__plc__=pl_checker
        self.subscribe(pl_checker)
        self.internet_checker = internet_checker
        if internet_checker:
            self.subscribe(internet_checker)
        self.__internet_restored_count__=0
        self.proxy_list:list[ProxyTestResult]=[]
        self.proxy_list_version=0
        self.checklist:list[ProxyInfo] = []
        self.bad_list:dict[str,ProxyInfo] = {}
        self.selected:ProxyTestResult=None
        self.selected_url:str=None
        self.__recheck_requested_for_version__=0

    def find_info(self, url:str)->ProxyInfo:
        r = self.bad_list.get(url)
        if r:
            return r
        for r in self.checklist:
            if r.url() == url:
                return r

    def _set_selected(self, selected:ProxyTestResult):
        if selected:
            if self.selected and self.selected.url == selected.url:
                self.selected = selected
                self.selected_url = selected.url
                return
            self.selected = selected
            self.selected_url = selected.url            
        else:
            if not self.selected:
                return
            self.selected = None
        self.notify_listeners()

    def _process(self):

        if self.internet_checker:
            internet_restored_count = self.internet_checker.restored_count
            if self.__internet_restored_count__ != internet_restored_count:
                self.bad_list={}
                self.__internet_restored_count__ = internet_restored_count

        check_results_version = self.__plc__.check_results_version
        if self.proxy_list_version != check_results_version:
            self.bad_list={}
            self.proxy_list = self.__plc__.check_results
            self.proxy_list_version = check_results_version

        found = True
        while len(self.checklist) < 3 and found:
            found = False
            for p in self.proxy_list:
                i = self.find_info(p.url)
                if not i:
                    p2 = ProxyInfo(p)
                    self.checklist.append(p2)
                    found = True
                    break

        if not found:
            if self.__recheck_requested_for_version__ != check_results_version:
                self.__plc__.check()
                self.__recheck_requested_for_version__ = check_results_version

        checklist:list[ProxyInfo]=[]
        for p in self.checklist:
            if self.reached(p.next_check):
                checklist.append(p)
        
        if len(checklist) > 0:
            for p, r in zip(checklist, check_proxies([p.url() for p in checklist])):
                suc = r==200
                p.set_check_result(suc)
                if p.is_bad():                    
                    self.checklist.remove(p)
                    self.bad_list[p.url]=p
                    if self.selected and self.selected.url == p.url():
                        self._set_selected(None)
                    self.signal()
                else:
                    p.next_check = self.schedule_delay(30 if suc else 10)

        if self.internet_checker and self.internet_checker.is_down or not self.internet_checker.is_whitelists:
            self._set_selected(None)
        else:
            if len(self.checklist) > 0:
                p = self.checklist[0]
                if p.is_good():
                    self._set_selected(p.proxy)

    def get_status(self)->dict:
        selected = self.selected
        return {
            "selected": selected.url if selected else None,
            "selected_quality": selected.quality if selected else None,
            "proxy_list": len(self.proxy_list),
            "checklist": len(self.checklist),
            "bad_list": len(self.bad_list),
        }

class ProxyProcessController(BaseProcess):

    def __init__(self, selector: ProxySelector):
        super().__init__()
        self.selector = selector
        if selector:
            self.subscribe(selector)
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
        config['inbounds'][0]['listen']='::'
        file_name = os.path.join(self._config_dir, "signbox_config.json")
        with open(file_name, 'w') as f:
            json.dump(config, f)
        return file_name

    def _process(self):

        if self.selector:
            self._proxy= self.selector.selected_url

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

    if True:

        internet_checker = InternetChecker()

        pl_loader = ProxyListLoader()
        proxy_checker = ProxyListChecker(pl_loader, internet_checker)
        proxy_selector = ProxySelector(proxy_checker, internet_checker)
        proxy_controller = ProxyProcessController(proxy_selector)

        pl_loader.start()
        proxy_checker.start()
        internet_checker.start()
        proxy_selector.start()
        proxy_controller.start()

        class HTTPServer(SimpleHTTPRequestHandler):

            def do_GET(self):
                if self.path == '/' or self.path == '/status':
                    res = {
                        "InternetChecker": internet_checker.get_status(),
                        "ProxyListLoader": pl_loader.get_status(),
                        "ProxyListChecker": proxy_checker.get_status(),
                        "ProxySelector": proxy_selector.get_status(),
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

    else:
        internet_checker = WhitelistChecker()

        pll = ProxyListLoader()
        ch = ProxyListChecker(pll, internet_checker)
        sel = ProxySelector(ch)
        proc = ProxyProcessController(sel)

        pll.start()
        ch.start()
        internet_checker.start()
        sel.start()
        proc.start()

        pll.join(1000)

