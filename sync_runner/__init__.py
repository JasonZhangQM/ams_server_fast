# -*- coding: utf-8 -*-
"""批量同步公共包：CLI 入口和 notebook 共用的功能模块。

CLI 脚本（run2_batch_sync.py）和 notebook（run2-批量同步.ipynb）都从此包
import，保证限速 / 重试 / 当日标记等核心逻辑只有一份源码。
"""

from .config import *
from .api import *
from .sync import *
from .executor import *
