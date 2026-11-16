from django.apps import AppConfig
from django.urls import register_converter


class ServersConfig(AppConfig):
    name = "servers"

    def ready(self):
        from .ids import ServerIdConverter
        register_converter(ServerIdConverter, "sid")           # <sid:pk> in the URL patterns: see servers/ids.py
