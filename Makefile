PYTHON ?= python3

.PHONY: help serve native check check-web check-native test smoke install uninstall

help:
	@echo "NetworkMap development commands"
	@echo "  make serve      Start the web server on 127.0.0.1:8765"
	@echo "  make native     Open the GTK/WebKit desktop shell"
	@echo "  make check      Run web and native dependency checks"
	@echo "  make check-web  Check the dependency-free server and scripts"
	@echo "  make check-native  Check the GTK/WebKit desktop runtime"
	@echo "  make test       Run the backend unit tests"
	@echo "  make smoke      Exercise a live server through its HTTP API"
	@echo "  make install    Install the app for the current user"
	@echo "  make uninstall  Recoverably remove the user-local installation"

serve:
	$(PYTHON) server.py --host 127.0.0.1 --port 8765

native:
	./networkmap

check: check-web check-native

check-web:
	$(PYTHON) -m py_compile server.py networkmap_sync.py scripts/smoke_test.py tests/test_server.py tests/test_native.py tests/test_sync.py
	$(PYTHON) server.py --help >/dev/null
	sh -n install.sh uninstall.sh

check-native:
	$(PYTHON) -m py_compile native.py
	$(PYTHON) native.py --help >/dev/null
	$(PYTHON) native.py --check
	sh -n networkmap

test: check-web
	$(PYTHON) -m unittest discover -s tests -p 'test_*.py' -v

smoke: check-web
	$(PYTHON) scripts/smoke_test.py

install:
	./install.sh

uninstall:
	./uninstall.sh
