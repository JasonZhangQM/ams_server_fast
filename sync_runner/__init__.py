# -*- coding: utf-8 -*-
"""批量同步公共包：后端批量同步（app/bds/router.py）的功能模块。

限速 / 重试 / 当日标记等核心逻辑只有一份源码，由后端后台线程调用。
"""

from .config import *
from .api import *
from .sync import *
from .executor import *
