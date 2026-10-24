import logging

from . import agent, config, notify, store, system, watch, web


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    store.prune()
    system.start()
    notify.start()
    watch.start()
    if config.ASSISTANT:
        agent.start()
    web.serve()


if __name__ == "__main__":
    main()
