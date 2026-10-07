import logging

from . import notify, store, system, watch, web


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    store.prune()
    system.start()
    notify.start()
    watch.start()
    web.serve()


if __name__ == "__main__":
    main()
