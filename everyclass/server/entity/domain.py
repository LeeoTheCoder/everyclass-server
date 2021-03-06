import datetime
from typing import Tuple, List

from everyclass.rpc import RpcClientException, RpcServerException, RpcTimeout
from everyclass.server.utils import base_exceptions
from everyclass.server.utils.config import get_config


def get_semester_date(date: datetime.date) -> Tuple[str, int, int]:
    """获取日期对应的学期、所属周次及星期（0表示周日，1表示周一...）

    >>> get_semester_date(datetime.date(2020, 2, 22))
    ('2019-2020-1', 26, 6)

    >>> get_semester_date(datetime.date(2020, 2, 23))
    ('2019-2020-2', 1, 0)
    """
    config = get_config()

    semesters = list(config.AVAILABLE_SEMESTERS.items())
    semesters.sort(key=lambda x: x[0], reverse=True)

    for sem in semesters:
        sem_start_date = datetime.date(*sem[1]["start"])
        if date >= sem_start_date:
            days_delta = (date - sem_start_date).days
            return "-".join([str(x) for x in sem[0]]), days_delta // 7 + 1, days_delta % 7
    raise ValueError("no applicable semester")


def semester_calculate(current_semester: str, semester_list: List[str]) -> List[Tuple[str, bool]]:
    """生成一个列表，每个元素是一个二元组，分别为学期字符串和是否为当前学期的布尔值"""
    available_semesters = []

    for each_semester in semester_list:
        if current_semester == each_semester:
            available_semesters.append((each_semester, True))
        else:
            available_semesters.append((each_semester, False))
    return available_semesters


def replace_exception(func):
    """将RPC模块的错误类型替换成业务类型的错误"""

    def _func(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except RpcClientException as e:
            raise base_exceptions.InvalidRequestException(repr(e))
        except(RpcServerException, RpcTimeout) as e:
            raise base_exceptions.InternalError(repr(e))

    return _func
