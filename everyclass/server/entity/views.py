from collections import defaultdict
from typing import Dict, List, Tuple

from ddtrace import tracer
from flask import Blueprint, current_app as app, escape, flash, redirect, render_template, request, session, url_for

from everyclass.common.format import contains_chinese
from everyclass.common.time import get_day_chinese, get_time_chinese, lesson_string_to_tuple
from everyclass.server import logger
from everyclass.server.entity import service as entity_service
from everyclass.server.entity.domain import semester_calculate
from everyclass.server.utils.encryption import decrypt
from everyclass.server.utils.session import StudentSession
from everyclass.server.utils.web_consts import MSG_INVALID_IDENTIFIER, SESSION_LAST_VIEWED_STUDENT, URL_EMPTY_SEMESTER
from everyclass.server.utils.web_helpers import disallow_in_maintenance, url_semester_check, handle_exception_with_error_page, \
    check_permission

entity_bp = Blueprint('query', __name__)


@entity_bp.route('/query', methods=['GET', 'POST'])
@disallow_in_maintenance
def query():
    """
    All in one 搜索入口，可以查询学生、老师、教室，然后跳转到具体资源页面

    正常情况应该是 post 方法，但是也兼容 get 防止意外情况，提高用户体验

    埋点：
    - `query_resource_type`, 查询的资源类型: classroom, single_student, single_teacher, multiple_people, or not_exist.
    - `query_type`, 查询方式（姓名、学工号）: by_name, by_id, other
    """

    # if under maintenance, return to maintenance.html
    if app.config["MAINTENANCE"]:
        return render_template("maintenance.html")

    keyword = request.values.get('id')

    # todo 简拼积分制
    if not keyword or len(keyword) < 2:
        flash('请输入需要查询的姓名、学号、教工号或教室名称，长度不要小于2个字符')
        return redirect(url_for('main.main'))

    # 调用 api-server 搜索
    with tracer.trace('rpc_search'):
        try:
            rpc_result = entity_service.search(keyword)
        except Exception as e:
            return handle_exception_with_error_page(e)

    # 不同类型渲染不同模板
    if len(rpc_result.classrooms) >= 1:  # 优先展示教室
        # 我们在 kibana 中使用服务名过滤 apm 文档，所以 tag 不用增加服务名前缀
        tracer.current_root_span().set_tag("query_resource_type", "classroom")
        tracer.current_root_span().set_tag("query_type", "by_name")

        if len(rpc_result.classrooms) > 1:  # 多个教室选择
            return render_template('entity/multipleClassroomChoice.html',
                                   name=keyword,
                                   classrooms=rpc_result.classrooms)
        return redirect(
            url_for("query.get_classroom",
                    url_rid=rpc_result.classrooms[0].room_id_encoded,
                    url_semester=rpc_result.classrooms[0].semesters[-1] if rpc_result.classrooms[0].semesters else URL_EMPTY_SEMESTER))
    elif len(rpc_result.students) == 1 and len(rpc_result.teachers) == 0:  # 一个学生
        tracer.current_root_span().set_tag("query_resource_type", "single_student")
        if contains_chinese(keyword):
            tracer.current_root_span().set_tag("query_type", "by_name")
        else:
            tracer.current_root_span().set_tag("query_type", "by_id")

        if len(rpc_result.students[0].semesters) < 1:
            flash('没有可用学期')
            return redirect(url_for('main.main'))

        return redirect(
            url_for("query.get_student",
                    url_sid=rpc_result.students[0].student_id_encoded,
                    url_semester=rpc_result.students[0].semesters[-1] if rpc_result.students[0].semesters else URL_EMPTY_SEMESTER))
    elif len(rpc_result.teachers) == 1 and len(rpc_result.students) == 0:  # 一个老师
        tracer.current_root_span().set_tag("query_resource_type", "single_teacher")
        if contains_chinese(keyword):
            tracer.current_root_span().set_tag("query_type", "by_name")
        else:
            tracer.current_root_span().set_tag("query_type", "by_id")

        if len(rpc_result.teachers[0].semesters) < 1:
            flash('没有可用学期')
            return redirect(url_for('main.main'))

        return redirect(
            url_for("query.get_teacher",
                    url_tid=rpc_result.teachers[0].teacher_id_encoded,
                    url_semester=rpc_result.teachers[0].semesters[-1] if rpc_result.teachers[0].semesters else URL_EMPTY_SEMESTER))
    elif len(rpc_result.teachers) >= 1 or len(rpc_result.students) >= 1:
        # multiple students, multiple teachers, or mix of both
        tracer.current_root_span().set_tag("query_resource_type", "multiple_people")

        if contains_chinese(keyword):
            tracer.current_root_span().set_tag("query_type", "by_name")

        else:
            tracer.current_root_span().set_tag("query_type", "by_id")

        students = []
        student_filtered = False
        for student in rpc_result.students:
            if len(student.klass) >= 6 and student.klass[-4:].isdigit() and int(student.klass[-4:-2]) <= 12:
                # 过滤<13级的学生
                student_filtered = True
                continue
            students.append(student)
        # todo 找校友功能
        return render_template('entity/peopleWithSameName.html',
                               name=keyword,
                               students=students,
                               teachers=rpc_result.teachers,
                               student_filtered=student_filtered)
    else:
        logger.info("No result for user search", extra={"keyword": request.values.get('id')})
        tracer.current_root_span().set_tag("query_resource_type", "not_exist")
        tracer.current_root_span().set_tag("query_type", "other")

        flash('没有找到任何有关 {} 的信息，如果你认为这不应该发生，请联系我们。'.format(escape(request.values.get('id'))))
        return redirect(url_for('main.main'))


@entity_bp.route('/student/<string:url_sid>/<string:url_semester>')
@entity_bp.route('/student/<string:url_sid>/semester/<string:url_semester>')
@url_semester_check
@disallow_in_maintenance
def get_student(url_sid: str, url_semester: str):
    """学生查询"""
    # decrypt identifier in URL
    try:
        _, student_id = decrypt(url_sid, resource_type='student')
    except ValueError:
        return render_template("common/error.html", message=MSG_INVALID_IDENTIFIER)

    if url_semester == URL_EMPTY_SEMESTER:
        try:
            student = entity_service.get_student(student_id)
        except Exception as e:
            return handle_exception_with_error_page(e)
    else:
        # RPC 获得学生课表
        try:
            student = entity_service.get_student_timetable(student_id, url_semester)
        except Exception as e:
            return handle_exception_with_error_page(e)

    # save sid_orig to session for verifying purpose
    # must be placed before privacy level check. Otherwise a registered user could be redirected to register page.
    session[SESSION_LAST_VIEWED_STUDENT] = StudentSession(sid_orig=student.student_id,
                                                          sid=student.student_id_encoded,
                                                          name=student.name)

    # 权限检查，如果没有权限则返回
    has_permission, return_val = check_permission(student)
    if not has_permission:
        return return_val

    if url_semester != URL_EMPTY_SEMESTER:
        with tracer.trace('process_rpc_result'):
            cards: Dict[Tuple[int, int], List[Dict[str, str]]] = dict()
            for card in student.cards:
                day, time = lesson_string_to_tuple(card.lesson)
                if (day, time) not in cards:
                    cards[(day, time)] = list()
                cards[(day, time)].append(card)
            empty_5, empty_6, empty_sat, empty_sun = _empty_column_check(cards)
            available_semesters = semester_calculate(url_semester, sorted(student.semesters))

        return render_template('entity/student.html',
                               have_semesters=True,
                               student=student,
                               cards=cards,
                               empty_sat=empty_sat,
                               empty_sun=empty_sun,
                               empty_6=empty_6,
                               empty_5=empty_5,
                               available_semesters=available_semesters,
                               current_semester=url_semester)
    else:
        # 无学期
        return render_template('entity/student.html',
                               have_semesters=False,
                               student=student)


@entity_bp.route('/teacher/<string:url_tid>/<string:url_semester>')
@entity_bp.route('/teacher/<string:url_tid>/semester/<string:url_semester>')
@disallow_in_maintenance
@url_semester_check
def get_teacher(url_tid, url_semester):
    """老师查询"""

    # decrypt identifier in URL
    try:
        _, teacher_id = decrypt(url_tid, resource_type='teacher')
    except ValueError:
        return render_template("common/error.html", message=MSG_INVALID_IDENTIFIER)

    if url_semester == URL_EMPTY_SEMESTER:
        # RPC to get teacher timetable
        try:
            teacher = entity_service.get_teacher(teacher_id)
        except Exception as e:
            return handle_exception_with_error_page(e)
    else:
        # RPC to get teacher timetable
        try:
            teacher = entity_service.get_teacher_timetable(teacher_id, url_semester)
        except Exception as e:
            return handle_exception_with_error_page(e)

    if url_semester != URL_EMPTY_SEMESTER:
        with tracer.trace('process_rpc_result'):
            cards = defaultdict(list)
            for card in teacher.cards:
                day, time = lesson_string_to_tuple(card.lesson)
                if (day, time) not in cards:
                    cards[(day, time)] = list()
                cards[(day, time)].append(card)

        empty_5, empty_6, empty_sat, empty_sun = _empty_column_check(cards)

        available_semesters = semester_calculate(url_semester, teacher.semesters)

        return render_template('entity/teacher.html',
                               have_semesters=True,
                               teacher=teacher,
                               cards=cards,
                               empty_sat=empty_sat,
                               empty_sun=empty_sun,
                               empty_6=empty_6,
                               empty_5=empty_5,
                               available_semesters=available_semesters,
                               current_semester=url_semester)
    else:
        # 无学期
        return render_template('entity/teacher.html',
                               have_semesters=False,
                               teacher=teacher)


@entity_bp.route('/classroom/<string:url_rid>/<string:url_semester>')
@entity_bp.route('/classroom/<string:url_rid>/semester/<string:url_semester>')
@url_semester_check
@disallow_in_maintenance
def get_classroom(url_rid, url_semester):
    """教室查询"""
    # decrypt identifier in URL
    try:
        _, room_id = decrypt(url_rid, resource_type='room')
    except ValueError:
        return render_template("common/error.html", message=MSG_INVALID_IDENTIFIER)
    # todo 支持没有学期的room
    # RPC to get classroom timetable
    try:
        room = entity_service.get_classroom_timetable(url_semester, room_id)
    except Exception as e:
        return handle_exception_with_error_page(e)

    with tracer.trace('process_rpc_result'):
        cards = defaultdict(list)
        for card in room.cards:
            day, time = lesson_string_to_tuple(card.lesson)
            cards[(day, time)].append(card)

    empty_5, empty_6, empty_sat, empty_sun = _empty_column_check(cards)

    available_semesters = semester_calculate(url_semester, room.semesters)

    return render_template('entity/room.html',
                           room=room,
                           cards=cards,
                           empty_sat=empty_sat,
                           empty_sun=empty_sun,
                           empty_6=empty_6,
                           empty_5=empty_5,
                           available_semesters=available_semesters,
                           current_semester=url_semester)


@entity_bp.route('/card/<string:url_cid>/<string:url_semester>')
@entity_bp.route('/card/<string:url_cid>/semester/<string:url_semester>')
@url_semester_check
@disallow_in_maintenance
def get_card(url_cid: str, url_semester: str):
    """课程查询"""
    # decrypt identifier in URL
    try:
        _, card_id = decrypt(url_cid, resource_type='klass')
    except ValueError:
        return render_template("common/error.html", message=MSG_INVALID_IDENTIFIER)

    # RPC to get card
    try:
        card = entity_service.get_card(url_semester, card_id)
    except Exception as e:
        return handle_exception_with_error_page(e)

    day, time = lesson_string_to_tuple(card.lesson)

    # cotc_id = COTeachingClass.get_id_by_card(card)
    # course_review_doc = CourseReview.get_review(cotc_id)

    return render_template('entity/card.html',
                           card=card,
                           card_day=get_day_chinese(day),
                           card_time=get_time_chinese(time),
                           # cotc_id=cotc_id,
                           # cotc_rating=course_review_doc["avg_rate"],
                           cotc_id=0,
                           cotc_rating=0,
                           current_semester=url_semester
                           )


@entity_bp.route('/available_rooms')
def available_rooms():
    return render_template("entity/available_rooms.html")


@entity_bp.route('/multi_people_schedule')
def multi_people_schedule():
    return render_template("entity/multi_people_schedule.html")


def _empty_column_check(cards: dict) -> Tuple[bool, bool, bool, bool]:
    """检查是否周末和晚上有课，返回三个布尔值"""
    with tracer.trace('_empty_column_check'):
        # 空闲周末判断，考虑到大多数人周末都是没有课程的
        empty_sat = True
        for cls_time in range(1, 7):
            if (6, cls_time) in cards:
                empty_sat = False

        empty_sun = True
        for cls_time in range(1, 7):
            if (7, cls_time) in cards:
                empty_sun = False

        # 空闲课程判断，考虑到大多数人11-12节都是没有课程的
        empty_6 = True
        for cls_day in range(1, 8):
            if (cls_day, 6) in cards:
                empty_6 = False
        empty_5 = True
        for cls_day in range(1, 8):
            if (cls_day, 5) in cards:
                empty_5 = False
    return empty_5, empty_6, empty_sat, empty_sun
