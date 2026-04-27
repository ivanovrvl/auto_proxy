import os
import time
import requests
import json
from threading import Event, Thread, Lock

class BaseProcess:
    pass

class BaseProcess:

    def __init__(self):
        self._event = Event()
        self._thread = Thread(target=self.__process_internal__)
        self._thread.setDaemon(True)
        self._listeners = []
        self.__next_start__ = None
        self.__signaled__ = True
        self.__last_error__ = None
        self.__last_error_at__ = None
        self.sleep_on_error = 60
        self._iddle_timeout = 60 # или None

    def _on_error(self, e:Exception):
        print(str(e))
        if self.sleep_on_error > 0:
            time.sleep(self.sleep_on_error)

    def _process(self):
        pass

    def schedule_at(self, t:float):
        if self.__next_start__ is None or self.__next_start__ > t:
            self.__next_start__ = t

    def reached(self, t:float):
        if t and t > time.time():
            self.schedule_at(t)
            return False
        return True

    def schedule_delay(self, delay:float)->float:
        t = time.time() + delay
        self.schedule_at(t)
        return t

    def _process_internal(self):
        self._process()

    def __process_internal__(self):
        while True:
            if not self.__signaled__ and not self._event.is_set():
                if self.__next_start__ is None:
                    self._event.wait(timeout=self._iddle_timeout)
                else:
                    timeout = self.__next_start__ - time.time()
                    if timeout > 0:
                        self._event.wait(timeout=timeout)
            self.__signaled__ = True
            self._event.clear()
            try:
                self.__next_start__ = None
                self._process_internal()
                self.__signaled__ = False
            except Exception as e:
                self.__last_error__ = e
                self.__last_error_at__ = time.time()
                self._on_error(e)

    def start(self):
        self._thread.start()

    def join(self, timeout=None):
        self._thread.join(timeout=timeout)

    def get_last_error(self):
        return self.__last_error__

    def get_last_at(self):
        return self.__last_error_at__

    def signal(self):
        with self.__lock__:
            self.__signaled__ += 1
        self._event.set()

    def add_listener(self, listener:Event):
        self._listeners.append(listener)

    def remove_listener(self, listener:Event):
        self._listeners.remove(listener)

    def notify_listeners(self):
        for o in self._listeners:
            if isinstance(o, BaseProcess):
                o.signal()
            elif isinstance(o, Event):
                o.set()

    def subscribe(self, process:BaseProcess):
        process.add_listener(self)

def download_if_modified(url, local_path, timeout=30):
    """
    Скачивает файл по URL и сохраняет в local_path, используя условные запросы HTTP.
    Если файл уже существует и не изменился на сервере, скачивание не выполняется.
    """
    # Проверяем существование основного файла
    file_exists = os.path.exists(local_path)
    meta_path = local_path + '.meta'
    headers = {}

    # Если файл существует, пытаемся загрузить метаданные для условного запроса
    if file_exists and os.path.exists(meta_path):
        try:
            with open(meta_path, 'r') as f:
                meta = json.load(f)
            if 'last_modified' in meta:
                headers['If-Modified-Since'] = meta['last_modified']
            if 'etag' in meta:
                headers['If-None-Match'] = meta['etag']
        except (json.JSONDecodeError, IOError):
            # Если метафайл поврежден, игнорируем его
            pass

    # Выполняем GET-запрос с потоковой передачей
    response = requests.get(url, headers=headers, stream=True, timeout=timeout)
    response.raise_for_status()

    # Обработка ответа
    if response.status_code == 304:
        return False
    elif response.status_code == 200:
        # Создаем директорию для файла, если необходимо
        os.makedirs(os.path.dirname(os.path.abspath(local_path)), exist_ok=True)
        # Сохраняем содержимое
        with open(local_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
        # Сохраняем метаданные
        meta = {}
        if 'Last-Modified' in response.headers:
            meta['last_modified'] = response.headers['Last-Modified']
        if 'ETag' in response.headers:
            meta['etag'] = response.headers['ETag']
        with open(meta_path, 'w') as f:
            json.dump(meta, f)
        return True
    else:
        print(f"Unexpected status code: {response.status_code} {response.reason}")

if __name__ == '__main__':

    if False:
        file_name = 'WHITE-CIDR-RU-all.txt'
        url = f'https://raw.githubusercontent.com/igareck/vpn-configs-for-russia/refs/heads/main/{file_name}'

        print( download_if_modified(url, file_name) )


    else:

        class Test(BaseProcess):

            def __init__(self):
                super().__init__()
                self._next = None

            def _process(self):
                if self.reached(self._next):
                    print(time.time())
                    self._next = self.schedule_delay(3)


        t = Test()
        t.start()

        t.join(20)