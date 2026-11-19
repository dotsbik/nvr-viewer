#!/usr/bin/python3 -I
"""Запуск «Видеорегистратора». В пакете лежит как /usr/lib/nvrview/run.py."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

from nvrview.app import main  # noqa: E402

sys.exit(main())
