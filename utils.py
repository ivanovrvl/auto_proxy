import time
from threading import Event, Thread, Lock

class BaseProcess:
    pass

class BaseProcess:

    def __init__(self):
        self._event = Event()
        self._thread = Thread(target=self.__process_internal__)
        self._thread.setDaemon(True)
        self._listeners = []
        self.__next_process__ = None
        self.__lock__ = Lock()
        self.__signaled__ = 1
        self.__last_error__ = None
        self.__last_error_at__ = None
        self.sleep_on_error = 60

    def _on_error(self, e:Exception):
        print(str(e))
        if self.sleep_on_error > 0:
            time.sleep(self.sleep_on_error)

    def _process(self):
        pass

    def schedule_at(self, t:float):
        if self.__next_process__ is None or self.__next_process__ > t:
            self.__next_process__ = t

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
            self._event.clear()
            while self.__signaled__ == 0:
                if self.__next_process__ is not None:
                    dt = self.__next_process__ - time.time()
                    if dt <= 0:
                        self.signal()
                        break
                    self._event.wait(dt)
                else:
                    if False:
                        self._event.wait()
                    else:
                        if not self._event.wait(60):
                            self.signal()
                            break
                self._event.clear()
            try:
                was_signaled = self.__signaled__
                self.__next_process__ = None
                self._process_internal()
                with self.__lock__:
                    self.__signaled__ -= was_signaled
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


if __name__ == '__main__':

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