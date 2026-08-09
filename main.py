import random
import os
import time
from threading import Event
from utils import BaseProcess, Watchdog, download_if_modified
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

import config
import paho.mqtt.client as mqtt

debug = config.debug

socks_proxy_port = 2080
proxy = f"socks5://127.0.0.1:{socks_proxy_port}"
proxies = {"http":proxy, "https":proxy}

with open('route.json', 'r') as f:
    route_json = json.load(f)

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
        with SingBoxProxy(url, tun_auto_route=False) as p:
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
        with SingBoxProxy(url, tun_auto_route=False) as p:
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
        self.is_whitelists = None
        self.is_down = True
        self._next = None
        self.restored_count=0
        self.__debug_whitelists__ = False
        self.__last_check__ = None
        self.__last_whitelist__ = None
        self.__last_not_whitelist__ = None

    def _check_urls(self, urls:list[str])->bool:
        try:
            for url in urls:
                r = requests.get(url, timeout=15)
                return True
        except Exception as e:
            print(str(e))
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
        if is_whitelists:
            self.__last_whitelist__ = datetime.now()
        else:
            self.__last_not_whitelist__ = datetime.now()
        self.notify_listeners()

    def _process(self):
        if self.reached(self._next):
            self._next = self.schedule_delay(60)
            self.__last_check__ = datetime.now()
            self.set_down(not self._check_urls(["https://ya.ru", "https://lenta.ru"]))
            if not self.is_down:
                if self.__debug_whitelists__:
                    self.set_whitelists(True)
                else:
                    t1 = self._check_urls(["https://bmstu.ru"])
                    t2 = self._check_urls(["https://mephi.ru"])
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
            "debug_whitelists": self.__debug_whitelists__,
            "last_check": dt2str(self.__last_check__),
            "last_whitelist": str(self.__last_whitelist__),
            "last_not_whitelist": str(self.__last_not_whitelist__),
            "last_error": str(self.get_last_error()),
        }

    def set_debug_whitelists(self, debug_whitelists:bool):
        if self.__debug_whitelists__ == debug_whitelists:
            return
        self.__debug_whitelists__ = debug_whitelists
        if debug_whitelists:
            self.set_whitelists(True)
        else:
            self._next = None
        self.signal()


class ProxyListLoader(BaseProcess):

    def __init__(self):
        super().__init__()
        self.proxy_list:list[str]=None
        self.proxy_list_version = 0
        self._next=None
        self._load_started = None
        self._load_completed = None
        self._load_error = None

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
            self._load_started = datetime.now()
            file_name = 'WHITE-CIDR-RU-all.txt'
            try:
                if self._fetch_proxies(file_name):
                    self._load_proxies(file_name)
                self._next = self.schedule_delay(15*60)
                self._load_error = None
                self._load_completed = datetime.now()
            except Exception as e:
                self._load_error = e
                self._load_completed = datetime.now()
                self._next = self.schedule_delay(30)

            if self.proxy_list is None:
                self._load_started = datetime.now()
                try:
                    self._load_proxies(file_name)
                    self._load_error = None
                    self._load_completed = datetime.now()
                except Exception as e:
                    self.proxy_list = []
                    self._load_error = e
                    self._load_completed = datetime.now()

    def load(self):
        self._next=None
        self.signal()

    def get_status(self)->dict:
        return {
            "load_started": dt2str(self._load_started),
            "load_completed": dt2str(self._load_completed),
            "load_error": str(self._load_error),
            "last_error": str(self.get_last_error()),
        }

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
        self.__save_check_results__=False

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
            self._check_completed = datetime.now()
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
        self.__save_check_results__=False

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

        if config.persist_checklist and not self.__loaded__:
            self.__load__()
            self.__loaded__ = True

        whitelists = self.internet_checker.is_whitelists
        internet_restored = self.internet_checker.restored_count
        if self._last_whitelist != whitelists or self.__internet_restored__ != internet_restored:
            self._next_check = None
            self.__internet_restored__ = internet_restored

        proxy_list_version = self.proxy_loader.proxy_list_version
        if self.last_proxy_version != proxy_list_version:
            self._next_check = None
            self.last_proxy_version = proxy_list_version

        if not self.internet_checker.is_down and self.reached(self._next_check):
            if self._process_check():
                self._last_whitelist = whitelists
                self.check_results_version += 1
                self.notify_listeners()
                if config.persist_checklist:
                    self.__save__()
                self._next_check = self.schedule_delay(3600)

        if self.__save_check_results__ and len(self.check_results) != 0:
            self.__save__()

    def save_check_results(self):
        if self.self.__save_check_results__:
            return
        self.self.__save_check_results__=True
        self.signal()

class ProxyInfo:

    def __init__(self, proxy:ProxyTestResult):
        self.proxy = proxy
        self.clear()

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

    def clear(self):
        self.fail=0
        self.success=0
        self.last_result:bool=False
        self.result_seq=0
        self.next_check=None

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
        self.selected_type = None
        
        self._primary_proxy = None
        if config.primary_proxy:
            t = ProxyTestResult()
            t.url = config.primary_proxy
            t.quality = 10
            t.is_ok = True
            self._primary_proxy = ProxyInfo(t)

    def find_info(self, url:str)->ProxyInfo:
        r = self.bad_list.get(url)
        if r:
            return r
        for r in self.checklist:
            if r.url() == url:
                return r

    def _set_selected(self, selected:ProxyTestResult, typ:str=None):
        if selected:
            if self.selected and self.selected.url == selected.url and self.selected_type == typ:
                return
            self.selected = selected
            self.selected_url = selected.url
            self.selected_type = typ
        else:
            if not self.selected and self.selected_type == typ:
                return
            self.selected_url = None
            self.selected_type = typ
            self.selected = None            
        self.notify_listeners()

    def _process(self):

        if self.internet_checker and (self.internet_checker.is_down or not self.internet_checker.is_whitelists):
            self.bad_list.clear()
            self.checklist.clear()
            if self.internet_checker.is_down:
                self._set_selected(None, "Нет интернета")
            else:
                self._set_selected(None, "Нет БС")
        else:

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
            while len(self.checklist) < config.checklist_size and found:
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
                    self.bad_list={}
                    self.__recheck_requested_for_version__ = check_results_version

            checklist:list[ProxyInfo]= []
            if self._primary_proxy:
                checklist.append(self._primary_proxy)
            for p in self.checklist:
                if self.reached(p.next_check):
                    checklist.append(p)

            if len(checklist) > 0:
                for p, r in zip(checklist, check_proxies([p.url() for p in checklist])):
                    suc = r==200
                    p.set_check_result(suc)
                    if p != self._primary_proxy:
                        if p.is_bad():
                            self.checklist.remove(p)
                            self.bad_list[p.url]=p
                            if self.selected and self.selected.url == p.url():
                                self._set_selected(None, "Нет публичных прокси")
                            self.signal()
                        else:
                            p.next_check = self.schedule_delay(30 if suc else 5)

            if self.internet_checker and self.internet_checker.is_down or not self.internet_checker.is_whitelists:
                self._set_selected(None, "Нет интернета")
            else:
                if self._primary_proxy and not self._primary_proxy.is_bad():
                    self._set_selected(self._primary_proxy.proxy, "WB Streams")
                elif len(self.checklist) > 0:
                    p = self.checklist[0]
                    if p.is_good():
                        self._set_selected(p.proxy, "Публичная прокси")

    def get_status(self)->dict:
        selected = self.selected
        return {
            "selected": selected.url if selected else None,
            "selected_quality": selected.quality if selected else None,
            "proxy_list": len(self.proxy_list),
            "checklist": len(self.checklist),
            "bad_list": len(self.bad_list),
            "last_error": str(self.get_last_error()),
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
        self._singbox = SingBoxProxy(None, tun_auto_route=False, config_only=True)
        self._process_started = None
        self._process_stopped = None
        self._last_config = None

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
        self._singbox.http_port = 2082
        self._singbox.config_url = proxy_url
        config = self._singbox.generate_config()
        for inbound in config['inbounds']:
            inbound['listen']='::'
        tls = config['outbounds'][0].get('tls')
        if tls:
            tls['insecure']=True
        if route_json is not None:
            config['route'] = route_json

        file_name = os.path.join(self._config_dir, "signbox_config.json")
        if self._last_config != config:
            with open(file_name, 'w') as f:
                json.dump(config, f, indent=4)
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
            config_file = self._make_config(self._used_proxy if self._used_proxy else config.default_proxy)
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

class MqttSender(BaseProcess):

    def __init__(self, config:dict, internet_checker:InternetChecker, proxy_selector:ProxySelector):
        super().__init__()
        self.internet_checker = internet_checker
        self.proxy_selector = proxy_selector
        self.subscribe(internet_checker)
        self.subscribe(proxy_selector)
        self.config = config
        self._started = False
        self._state = None
        self._next_send = None

    def _connect(self, client:mqtt.Client):
        client.connect(self.config["address"], self.config.get("port", 1883), 60)

    def _is_started(self):
        if not self._started:
            mq = mqtt.Client()
            try:
                self._connect(mq)
                mq.publish("ha/sensor/internet_wl/config", 
                    """{
                    "name": "Белые списки",
                    "unique_id": "internet_wl",
                    "state_topic": "dacha/internet",
                    "payload_on": "on",
                    "payload_off": "off",
                    "value_template": "{{ value_json.wl }}",
                    "state_on": "on",
                    "state_off": "off",
                    "retain": true
                    }""" 
                )
                mq.publish("ha/sensor/internet_proxy/config", 
                    """{
                    "name": "Интернет",
                    "unique_id": "internet_proxy",
                    "state_topic": "dacha/internet",
                    "value_template": "{{ value_json.type }}",
                    "retain": true
                    }""" 
                )                
                self._started = True
            finally:
                mq.disconnect()
        return self._started

    def _process(self):
        if self._is_started():
            state = json.dumps({
                "wl" : 'on' if self.internet_checker.is_whitelists else 'off',
                "proxy_url" : self.proxy_selector.selected_url,
                "type": self.proxy_selector.selected_type
            })
            if state != self._state or self.reached(self._next_send):                                
                mq = mqtt.Client()
                try:
                    self._connect(mq)
                    mq.publish("dacha/internet", state)
                    self._state = state
                finally:
                    mq.disconnect()
                self._next_send = self.schedule_delay(300)
                self._state = state

class Debug(BaseProcess):

    def __init__(self, internet_checker:InternetChecker):
        super().__init__()
        self.internet_checker = internet_checker
        self._start_wl = self.schedule_delay(5)

    def _process(self):
        if self._start_wl and self.reached(self._start_wl):
            self.internet_checker.set_debug_whitelists(True)
            self._start_wl = None

if __name__ == '__main__':

    kill_signbox_processes()

    if True:

        internet_checker = InternetChecker()

        pl_loader = ProxyListLoader()
        proxy_checker = ProxyListChecker(pl_loader, internet_checker)
        proxy_selector = ProxySelector(proxy_checker, internet_checker)
        proxy_controller = ProxyProcessController(proxy_selector)
        whatchdog = Watchdog(proxy_checker, 10*60, 30*60)
        c = getattr(config, 'mqtt', None)
        mqtt_sender = MqttSender(c, internet_checker, proxy_selector) if c else None

        pl_loader.start()
        proxy_checker.start()
        internet_checker.start()
        proxy_selector.start()
        proxy_controller.start()
        whatchdog.start()
        if mqtt_sender:
            mqtt_sender.start()

        if config.debug:
            debug = Debug(internet_checker)
            debug.start()

        def send_default_headers(self):
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")

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
                    if mqtt_sender:
                        res["MqttSender"] = mqtt_sender.get_status()
                    self.send_response(200)
                    send_default_headers(self)
                    self.send_header('Content-type', 'application/json')
                    self.end_headers()
                    data = json.dumps(res, ensure_ascii=True, indent=4).encode()
                    self.wfile.write(data)
                    return
                elif self.path == '/save_check_results':
                    proxy_checker.save_check_results()
                elif self.path == '/set_wl':
                    internet_checker.set_debug_whitelists(True)
                elif self.path == '/reset_wl':
                    internet_checker.set_debug_whitelists(False)
                elif self.path == '/proxies':
                    proxies = proxy_checker.check_results
                    self.send_response(200)
                    send_default_headers(self)
                    self.send_header('Content-type', 'text/plain')
                    self.end_headers()
                    self.wfile.write(('\n'.join([p.url for p in proxies])).encode())
                    return
                elif self.path == '/check':
                    proxy_checker.check()
                else:
                    self.send_response(404)
                    send_default_headers(self)
                    self.send_header('Content-type', 'application/json')
                    self.end_headers()
                    return
                self.send_response(301)
                self.send_header('Location', '/')
                self.end_headers()

        with socketserver.TCPServer(("", 2081), HTTPServer) as httpd:
            httpd.allow_reuse_address = True
            httpd.serve_forever()

    else:
        internet_checker = InternetChecker()
        internet_checker.start()

        config = {
            "address": "192.168.1.1"
        }
        sender = MqttSender(config, internet_checker)
        sender.start()

        internet_checker.join(1000)

