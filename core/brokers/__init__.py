"""Concrete BrokerAdapter implementations (see core/broker.py for the interface).

Each module imports its vendor SDK lazily, so importing this package never
requires ib_async or ctrader-open-api to be installed.
"""
