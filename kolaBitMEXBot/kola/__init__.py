# https://packaging.python.org/guides/packaging-namespace-packages/
# probably not really  usefull  for me
__path__ = __import__("pkgutil").extend_path(__path__, __name__)
