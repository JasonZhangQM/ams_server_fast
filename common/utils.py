# -*- coding: utf-8 -*-
"""通用工具函数（从 server_dj/common/utils.py 迁移）。

迁移要点：
- 所有与 Django 无关的函数原样保留（函数名、入参、逻辑不变）。
- get_field_max_sql 中将 Django 的 mdl._meta.db_table 改为 SQLAlchemy 的 mdl.__table__.name。
- df_init_model 依赖 BaseModel 的 map_fields/db_fields/to_dtype 方法，保持原逻辑（含 df.columns 用法）。
"""
from sqlalchemy import text
import codecs
import pandas as pd
import numpy as np
from functools import wraps
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError


def filter_in_cols(df_cols: list[str], complete_cols: list[str]) -> list[str]:
    """过滤出 df_cols 中存在的 complete_cols 列。"""
    in_cols = []
    for col in complete_cols:
        if col in df_cols:
            in_cols.append(col)
    return in_cols


def map_value(s, multiplier: dict):
    """判断字符串是否以某 key 开头，返回对应的映射值；无匹配则返回 1。"""
    s_str = str(s)  # 确保输入是字符串（避免非字符串类型报错）
    for key, value in multiplier.items():
        if s_str.startswith(key):  # 判断字符串是否以 key 开头
            return value
    return 1


def filter_dtypes(df_cols: list, dtypes_dict: dict) -> dict:
    """在 dtypes_dict 中筛选 df_cols 对应的数据类型，多余的舍弃。"""
    dtype_dict = {}
    for col in df_cols:  # 筛选预设值的数据类型
        try:
            dtype_dict[col] = dtypes_dict[col]
        except KeyError:  # col 不在预设值的数据类型字典中
            pass
    return dtype_dict


def df_init_model(df, mdl, is_id=False):
    """导入数据到 model：清洗、列名映射、字段过滤、类型转换、空值适配。"""
    df_copy = df.copy()
    df_copy = df_copy.map(
        lambda x: x.strip() if isinstance(x, str) else x)  # 去除空格
    df_copy = df_copy.rename(columns=mdl.map_fields())  # 外部列名映射数据库字段
    df_copy = df_copy[filter_in_cols(df_copy.columns, mdl.db_fields(is_id))]  # 过滤字段
    # 注意：此处用 df.columns（原始列名）保持与原 Django 逻辑一致
    df_copy = df_copy.astype(filter_dtypes(df.columns, mdl.to_dtype()))  # 转换数据类型
    df_copy = df_copy.replace({np.nan: None})  # 空值适配数据库
    return df_copy


def act_sql_engine(engine, sql: str, params=None):
    """执行 sqlalchemy 的 engine 语句，返回受影响行数。"""
    with engine.connect() as conn:
        try:
            result = conn.execute(
                text(sql),  # text() 包装 SQL，避免 ObjectNotExecutableError
                params,  # 传入字典列表
            )
            conn.commit()
            inserted_rows = result.rowcount  # 实际插入的行数（排除重复）
            return inserted_rows
        except Exception as e:
            conn.rollback()
            raise e


def upsert_df_to_db(df: pd.DataFrame, table: str, engine, unique_keys: list, update_columns: list = None):
    """基于联合唯一键执行 UPSERT。

    :param df: 待同步 DataFrame
    :param table: 表名
    :param engine: 数据库连接引擎
    :param unique_keys: 联合唯一键列表（如 ["user_id", "order_no"]）
    :param update_columns: 需更新的列；为 None 时默认更新除联合唯一键外的所有列；
        传空列表 [] 表示仅插入、已存在记录不更新（使用 INSERT IGNORE）
    """
    df = df.replace({np.nan: None})
    columns = df.columns.tolist()
    if update_columns is None:  # 未指定更新列时，默认更新除联合唯一键外的所有列
        update_columns = [col for col in columns if col not in unique_keys]

    cols_str = ', '.join(columns)
    vals_str = ', '.join([f':{col}' for col in columns])
    if update_columns:  # 有需更新的列：ON DUPLICATE KEY UPDATE
        update_str = ", ".join([f"{col}=VALUES({col})" for col in update_columns])
        sql = f'''
            INSERT INTO {table} ({cols_str})
            VALUES ({vals_str})
            ON DUPLICATE KEY UPDATE {update_str}
            '''
    else:  # 无需更新的列：INSERT IGNORE（已存在记录跳过，不更新任何字段）
        sql = f'''
            INSERT IGNORE INTO {table} ({cols_str})
            VALUES ({vals_str})
            '''
    # 将 DataFrame 转为字典列表（每个字典的键是列名，值是对应行数据）
    data_dicts = df.to_dict('records')  # records 参数输出列名为键,每行为一个字典的列表
    result = act_sql_engine(engine, sql, data_dicts)
    return result


def get_sql_to_df(sql: str, engine) -> pd.DataFrame:
    """数据库查询数据并转换为 DataFrame。"""
    with engine.connect() as conn:
        sql = text(sql)  # 用 text() 包装 SQL 字符串
        result = conn.execute(sql)  # 执行查询
        columns = result.keys()  # 获取列名和数据
        df = pd.DataFrame(result.fetchall(), columns=columns)
        return df


def get_field_max_sql(field, mdl, engine):
    """读取指定数据库表中指定 field 的最大值。

    迁移变更：mdl._meta.db_table -> mdl.__table__.name（SQLAlchemy 元数据）
    """
    sql = f'''
        SELECT MAX({field}) as field_max
        FROM {mdl.__table__.name}
    '''
    with engine.connect() as conn:
        sql = text(sql)  # 用 text() 包装 SQL 字符串
        result = conn.execute(sql)  # 执行查询
        return result.fetchone()[0]  # 获取查询结果的第一行


def write_text_to_file(file_path, content, mode='w'):
    """将文本内容写入指定文件。

    :param file_path: 文件路径
    :param content: 要写入的文本内容
    :param mode: 打开文件的模式，默认 'w'(覆盖写入)，可选 'a'(追加写入)
    """
    try:
        with open(file_path, mode, encoding='utf-8') as file:
            file.write(content)
        print(f"成功将内容写入文件: {file_path}")
    except IOError as e:
        print(f"写入文件时发生错误: {e}")
    except Exception as e:
        print(f"发生意外错误: {e}")


def detect_file_encoding(file_path, candidate_encodings=['gbk', 'gb2312', 'utf-8', 'latin-1']):
    """自动检测文件编码格式（优先尝试中文常见编码）。

    :param file_path: TXT 文件路径
    :param candidate_encodings: 待尝试的编码列表
    :return: 成功读取的编码格式；若均失败则抛出 ValueError
    """
    for encoding in candidate_encodings:
        try:
            with open(file_path, 'r', encoding=encoding) as f:
                f.read(1024)  # 读取前 1024 字节测试编码
            print(f"成功检测到文件编码：{encoding}")
            return encoding
        except UnicodeDecodeError:
            continue
        except FileNotFoundError:
            raise FileNotFoundError(f"文件不存在：{file_path}")
    raise ValueError("未检测到支持的编码格式，请确认文件是否为文本文件")


def extract_futures_deposit(file_path):
    """从期货结算单 TXT 文件中提取出入金记录，转换为 DataFrame（适配中文编码）。"""
    # 1. 自动检测并读取文件（解决编码问题）
    encoding = detect_file_encoding(file_path)
    with open(file_path, 'r', encoding=encoding) as f:
        lines = f.readlines()

    # 2. 定位出入金记录的起始和结束位置（基于文档特征）
    start_flag = "发生日期"  # 成交记录表头标识
    end_flag = "出入金---Deposit/Withdrawal"  # 出入金记录尾部标识
    start_idx = None
    end_idx = None

    for i, line in enumerate(lines):
        if start_flag in line:
            header_idx = i
            start_idx = i + 3  # 跳过表头和分隔线，定位到第一条数据行
        if end_idx is None and start_idx is not None and end_flag in line:
            end_idx = i - 2  # 跳过尾部统计行和分隔线，定位到最后一条数据行
            break
    # 检查是否找到出入金记录区域
    if start_idx is None or end_idx is None:
        print("未在文件中找到交易记录区域，请确认文件格式是否正确")
    else:
        # 3. 提取出入金记录数据行（过滤空行和分隔线）
        transaction_lines = []
        for line in lines[start_idx:end_idx + 1]:
            line_stripped = line.strip()
            # 排除分隔线（全为"-"或"|"的行）、统计行和空行
            if not (line_stripped.startswith("---") or line_stripped.startswith("|共") or line_stripped == ""):
                transaction_lines.append(line_stripped)

        header_line = lines[header_idx]
        columns = [header.strip() for header in header_line.split("|") if header.strip() != ""]
        columns.remove('汇率')
        # 4. 解析每行数据（按"|"分割，清理空格和空字符串）
        data = []
        for line in transaction_lines:
            parts = [part.strip() for part in line.split("|") if part.strip() != ""]
            if len(parts) == len(columns):
                data.append(parts)
            else:
                print(f"警告：跳过格式异常的行（字段数不匹配）：{line[:50]}...")
        # 5. 转换为 DataFrame 并处理数据类型（数值/日期字段格式化）
        df = pd.DataFrame(data, columns=columns)

        # 数值字段转为 float/int（处理交易金额、手续费等）
        numeric_fields = ["入金", "出金"]
        for field in numeric_fields:
            df[field] = pd.to_numeric(df[field], errors="coerce")  # 错误值转为 NaN

        # 日期字段转为 datetime 类型（适配 "20250826" 格式）
        df["发生日期"] = pd.to_datetime(df["发生日期"], format="%Y%m%d", errors="coerce")
        return df


def extract_futures_transactions(file_path):
    """从期货结算单 TXT 文件中提取成交记录，转换为 DataFrame（适配中文编码）。

    :param file_path: TXT 文件路径
    :return: 包含成交记录的结构化 DataFrame
    """
    # 1. 自动检测并读取文件（解决编码问题）
    encoding = detect_file_encoding(file_path)
    with open(file_path, 'r', encoding=encoding) as f:
        lines = f.readlines()

    # 2. 定位成交记录的起始和结束位置（基于文档特征）
    start_flag = "成交日期"  # 成交记录表头标识
    end_flag = "能源中心---INE  上期所---SHFE"  # 成交记录尾部标识
    start_idx = None
    end_idx = None

    for i, line in enumerate(lines):
        if start_flag in line:
            header_idx = i
            start_idx = i + 3  # 跳过表头和分隔线，定位到第一条数据行
        if end_idx is None and start_idx is not None and end_flag in line:
            end_idx = i - 2  # 跳过尾部统计行和分隔线，定位到最后一条数据行
            break
    # 检查是否找到成交记录区域
    if start_idx is None or end_idx is None:
        raise ValueError("未在文件中找到成交记录区域，请确认文件格式是否正确")

    # 3. 提取成交记录数据行（过滤空行和分隔线）
    transaction_lines = []
    for line in lines[start_idx:end_idx + 1]:
        line_stripped = line.strip()
        if not (line_stripped.startswith("---") or line_stripped.startswith("|共") or line_stripped == ""):
            transaction_lines.append(line_stripped)

    header_line = lines[header_idx]
    columns = [header.strip() for header in header_line.split("|") if header.strip() != ""]

    # 4. 解析每行数据（按"|"分割，清理空格和空字符串）
    data = []
    for line in transaction_lines:
        parts = [part.strip() for part in line.split("|") if part.strip() != ""]
        if len(parts) == len(columns):
            data.append(parts)
        else:
            print(f"警告：跳过格式异常的行（字段数不匹配）：{line[:50]}...")
    # 5. 转换为 DataFrame 并处理数据类型（数值/日期字段格式化）
    df = pd.DataFrame(data, columns=columns)

    # 数值字段转为 float/int（处理交易金额、手续费等）
    numeric_fields = ["成交价", "手数", "成交额", "手续费", "平仓盈亏", "权利金收支"]
    for field in numeric_fields:
        df[field] = pd.to_numeric(df[field], errors="coerce")

    # 日期字段转为 datetime 类型（适配 "20250826" 格式）
    df["成交日期"] = pd.to_datetime(df["成交日期"], format="%Y%m%d", errors="coerce")

    return df


def convert_to_utf8(input_file, output_file, input_encoding='gbk'):
    """将文本文件转换为 UTF-8 编码。

    :param input_file: 输入文件路径
    :param output_file: 输出文件路径
    :param input_encoding: 输入文件的原始编码，默认为 GBK
    """
    try:
        with codecs.open(input_file, 'r', encoding=input_encoding) as f:
            content = f.read()
        with codecs.open(output_file, 'w', encoding='utf-8') as f:
            f.write(content)
        print(f"文件已成功转换为UTF-8编码并保存至: {output_file}")
    except UnicodeDecodeError:
        print(f"错误: 无法以 {input_encoding} 编码解析文件，请尝试其他编码")
    except Exception as e:
        print(f"转换过程中发生错误: {str(e)}")


def call_with_timeout(func, timeout=10):
    """为可能阻塞的函数添加超时保护。

    gm 终端未启动时，current() 会无限阻塞等待连接，
    导致后端 worker 被占满。此包装函数在独立线程中执行目标函数，
    超时后抛出 TimeoutError，避免后端被卡死。

    :param func: 目标函数
    :param timeout: 超时秒数，默认 10 秒
    :return: 目标函数的返回值
    :raises TimeoutError: 超时未返回时抛出
    """
    @wraps(func)
    def wrapper(*args, **kwargs):
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(func, *args, **kwargs)
            try:
                return future.result(timeout=timeout)
            except FuturesTimeoutError:
                raise TimeoutError(
                    f"调用 {func.__name__} 超时（{timeout}秒），"
                    f"请检查 gm 终端服务是否启动")
    return wrapper


def fetch_json_with_timeout(url: str, params: dict, timeout: int = 30,
                            max_retries: int = 5) -> dict:
    """带超时保护和自动重试的 HTTP GET 请求，返回解析后的 JSON 字典。

    使用 httpx 同步客户端，通过 ThreadPoolExecutor 实现超时保护，
    与 call_with_timeout 思路一致，但专为带 url+params 的 HTTP 请求设计。
    针对网络抖动（如 FRED API 偶发 SSL EOF）采用指数退避重试。

    :param url: 请求 URL
    :param params: 查询参数字典
    :param timeout: 单次请求超时秒数，默认 30 秒
    :param max_retries: 最大重试次数，默认 3 次（含首次共 3 次）
    :return: 解析后的 JSON 字典
    :raises TimeoutError: 请求超时（重试后仍失败）
    :raises RuntimeError: HTTP 错误或 JSON 解析失败（重试后仍失败）
    """
    import httpx
    import ssl
    import time

    # 从项目配置读取代理（若 .env 中配置了 HTTP_PROXY/HTTPS_PROXY 则启用）
    # 用于访问被网络环境阻断的境外 API（如 IMF SDMX API: dataservices.imf.org）
    # httpx 0.28+ 已移除 proxies 参数，改用 proxy= 单一字符串
    try:
        from server_fast.config import settings as _settings
        _proxy = _settings.proxy
    except Exception:
        _proxy = None

    # 仅对可恢复的网络层错误重试；HTTP 4xx/5xx 属业务错误不重试
    _RETRYABLE_EXC = (httpx.TransportError, httpx.NetworkError,
                      ConnectionError, TimeoutError, OSError)

    # 构建 SSL 上下文：禁用 TLS 1.3，兼容部分服务器（如 FRED）的 TLS 重协商
    # 某些服务器在 TLS 1.3 握手时与 OpenSSL 存在兼容问题，降级到 TLS 1.2 可解决
    _ssl_ctx = ssl.create_default_context()
    _ssl_ctx.options |= ssl.OP_NO_TLSv1_3
    _ssl_ctx.set_ciphers('DEFAULT@SECLEVEL=1')

    def _do_request() -> dict:
        # verify 传入自定义 SSL 上下文；timeout 连接+读取各 30s
        # proxy 为 None 时 httpx 直连，配置后走代理
        with httpx.Client(timeout=timeout, verify=_ssl_ctx, proxy=_proxy) as client:
            resp = client.get(url, params=params)
            resp.raise_for_status()
            return resp.json()

    last_exc = None
    for attempt in range(1, max_retries + 1):
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_do_request)
            try:
                return future.result(timeout=timeout)
            except FuturesTimeoutError:
                last_exc = TimeoutError(f"HTTP 请求超时（{timeout}秒）：{url}")
            except httpx.HTTPStatusError as e:
                # HTTP 状态码错误不重试（业务层问题）
                raise RuntimeError(
                    f"HTTP 错误 {e.response.status_code}：{e.response.text[:200]}")
            except _RETRYABLE_EXC as e:
                last_exc = RuntimeError(f"请求失败：{url}，原因：{str(e)}")
            except Exception as e:
                # 其他异常（含 SSL EOF）按可重试处理
                last_exc = RuntimeError(f"请求失败：{url}，原因：{str(e)}")

        # 未成功且仍有重试次数：指数退避（1s, 2s, 4s...）
        if attempt < max_retries:
            backoff = 2 ** (attempt - 1)
            time.sleep(backoff)

    raise last_exc
