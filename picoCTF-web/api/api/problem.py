""" Module for interacting with the problems """
import imp
import json
import pymongo

import api

from datetime import datetime
from api.common import validate, check, safe_fail, InternalException, SevereInternalException, WebException
from voluptuous import Schema, Length, Required, Range
from pymongo.errors import DuplicateKeyError
from bson import json_util
from os.path import join, isfile

grader_base_path = "./graders"
check_graders_exist = True

submission_schema = Schema({
    Required("tid"): check(
        ("This does not look like a valid tid.", [str, Length(max=100)])),
    Required("pid"): check(
        ("This does not look like a valid pid.", [str, Length(max=100)])),
    Required("key"): check(
        ("This does not look like a valid key.", [str, Length(max=100)]))
})

problem_schema = Schema({
    Required("name"): check(
        ("The problem's display name must be a string.", [str])),
    Required("score"): check(
        ("Score must be a positive integer.", [int, Range(min=0)])),
    Required("category"): check(
        ("Category must be a string.", [str])),
    Required("grader"): check(
        ("The grader path must be a string.", [str])),
    Required("description"): check(
        ("The problem description must be a string.", [str])),
    Required("threshold"): check(
        ("Threshold must be a positive integer.", [int, Range(min=0)])),

    "disabled": check(
        ("A problem's disabled state is either True or False.", [
            lambda disabled: type(disabled) == bool])),
    "autogen": check(
        ("A problem should either be autogenerated or not, True/False", [
            lambda autogen: type(autogen) == bool])),
    "related_problems": check(
        ("Related problems should be a list of related problems.", [list])),
    "pid": check(
        ("You should not specify a pid for a problem.", [lambda _: False])),
    "weightmap": check(
        ("Weightmap should be a dict.", [dict])),
    "tags": check(
        ("Tags must be described as a list.", [list])),
    "hint": check(
        ("A hint must be a string.", [str])),

    "_id": check(
        ("Your problems should not already have _ids.", [lambda id: False]))
})

def analyze_problems():
    """
    Checks the sanity of inserted problems.
    Includes weightmap and grader verification.

    Returns:
        A list of error strings describing the problems.
    """

    grader_missing_error = "{}: Missing grader at '{}'."
    unknown_weightmap_pid = "{}: Has weightmap entry '{}' which does not exist."

    problems = get_all_problems()

    errors = []

    for problem in problems:
        if not isfile(join(grader_base_path, problem["grader"])):
            errors.append(grader_missing_error.format(problem["name"], problem["grader"]))

        for pid in problem["weightmap"].keys():
            if safe_fail(get_problem, pid=pid) is None:
                errors.append(unknown_weightmap_pid.format(problem["name"], pid))
    return errors

def insert_problem(problem):
    """
    Inserts a problem into the database. Does sane validation.

    Args:
        Problem dict.
        score: points awarded for completing the problem.
        category: problem's category
        description: description of the problem.
        grader: path relative to grader_base_path
        threshold: Amount of points necessary for a team to unlock this problem.

        Optional:
        disabled: True or False. Defaults to False.
        hint: hint for completing the problem.
        tags: list of problem tags.
        relatedproblems: list of related problems.
        weightmap: problem's unlock weightmap
        autogen: Whether or not the problem will be auto generated.
    Returns:
        The newly created problem id.
    """

    db = api.common.get_conn()
    validate(problem_schema, problem)

    problem["disabled"] = problem.get("disabled", False)

    problem["pid"] = api.common.hash(problem["name"])

    weightmap = {}

    if problem.get("weightmap"):
        for name, weight in problem["weightmap"].items():
            name_hash = api.common.hash(name)
            weightmap[name_hash] = weight

    problem["weightmap"] = weightmap

    if safe_fail(get_problem, pid=problem["pid"]) is not None:
        raise WebException("Problem with identical pid already exists.")

    if safe_fail(get_problem, name=problem["name"]) is not None:
        raise WebException("Problem with identical name already exists.")

    db.problems.insert(problem)

    return problem["pid"]

def remove_problem(pid):
    """
    Removes a problem from the given database.

    Args:
        pid: the pid of the problem to remove.
    Returns:
        The removed problem object.
    """

    db = api.common.get_conn()
    problem = get_problem(pid=pid)

    db.problems.remove({"pid": pid})

    return problem

def set_problem_disabled(pid, disabled):
    """
    Updates a problem's availability.

    Args:
        pid: the problem's pid
        disabled: whether or not the problem should be disabled.
    Returns:
        The updated problem object.
    """
    return update_problem(pid, {"disabled": disabled})

def update_problem(pid, updated_problem):
    """
    Updates a problem with new properties.

    Args:
        pid: the pid of the problem to update.
        updated_problem: an updated problem object.
    Returns:
        The updated problem object.
    """

    db = api.common.get_conn()

    problem = get_problem(pid=pid)
    problem.update(updated_problem)

    validate(problem_schema, problem)

    db.problems.update({"pid": pid}, problem)

    return problem

def search_problems(*conditions):
    """
    Aggregates all problems that contain all of the given properties from the list specified.

    Args:
        conditions: multiple mongo queries to search.
    Returns:
        The list of matching problems.
    """

    db = api.common.get_conn()

    return list(db.problems.find({"$or": list(conditions)}, {"_id":0}))

def insert_problem_from_json(blob):
    """
    Converts json blob of problem(s) into dicts. Runs insert_problem on each one.
    See insert_problem for more information.

    Returns:
        A list of the created problem pids if an array of problems is specified.
    """

    result = json_util.loads(blob)

    if type(result) == list:
        return [insert_problem(problem) for problem in result]
    elif type(result) == dict:
        return insert_problem(result)
    else:
        raise InternalException("JSON blob does not appear to be a list of problems or a single problem.")

def grade_problem(pid, key, uid=None):
    """
    Grades the problem with its associated grader script.

    Args:
        uid: uid if provided
        pid: problem's pid
        key: user's submission
    Returns:
        A dict.
        correct: boolean
        points: number of points the problem is worth.
        message: message returned from the grader.
    """

    problem = get_problem(pid=pid)

    try:
        (correct, message) = imp.load_source(
            problem["grader"][:-3], join(grader_base_path, problem["grader"])
        ).grade(uid, key)
    except FileNotFoundError:
        raise WebException("Problem grader for {} is offline.".format(get_problem(pid=pid)['name']))

    return {
        "correct": correct,
        "points": problem["score"],
        "message": message
    }

def submit_key(tid, pid, key, uid=None, ip=None):
    """
    User problem submission. Problem submission is inserted into the database.

    Args:
        tid: user's team id
        pid: problem's pid
        key: answer text
        uid: user's uid
    Returns:
        A dict.
        correct: boolean
        points: number of points the problem is worth.
        message: message returned from the grader.
    """

    db = api.common.get_conn()
    validate(submission_schema, {"tid": tid, "pid": pid, "key": key})

    if pid not in get_unlocked_pids(tid):
        raise InternalException("You can't submit flags to problems you haven't unlocked.")

    if pid in get_solved_pids(tid):
        raise WebException("You have already solved this problem.")

    user = api.user.get_user(uid=uid)
    if user is None:
        raise InternalException("User submitting flag does not exist.")
    uid = user["uid"]

    result = grade_problem(pid, key, uid)

    problem = get_problem(pid=pid)

    submission = {
        'uid': uid,
        'tid': tid,
        'timestamp': datetime.now(),
        'pid': pid,
        'ip': ip,
        'key': key,
        'category': problem['category'],
        'correct': result['correct']
    }

    if (key, pid) in [(submission["key"], submission["pid"]) for submission in  get_submissions(tid=tid)]:
        raise WebException("You or one of your teammates has already tried this solution.")

    db.submissions.insert(submission)

    return result

def get_submissions(pid=None, uid=None, tid=None, category=None):
    """
    Gets the submissions from a team or user.
    Optional filters of pid or category.

    Args:
        uid: the user id
        tid: the team id

        category: category filter.
        pid: problem filter.
    Returns:
        A list of submissions from the given entity
    """

    db = api.common.get_conn()

    match = {}

    if uid is not None:
      match.update({"uid": uid})
    elif tid is not None:
      match.update({"tid": tid})

    if pid is not None:
      match.update({"pid": pid})

    if category is not None:
      match.update({"category": category})

    return list(db.submissions.find(match, {"_id":0}))

def get_correct_submissions(pid=None, uid=None, tid=None, category=None):
    """
    Gets the correct submissions from a team or user.
    Optional filters of pid or category.

    Args:
        uid: the user id
        tid: the team id

        category: category filter.
        pid: problem filter.
    Returns:
        A list of submissions from the given entity.
    """

    db = api.common.get_conn()

    match = {"correct": True}

    if uid is not None:
        match.update({"uid": uid})
    elif tid is not None:
        match.update({"tid": tid})
    else:
        raise InternalException("Must specify uid or tid")

    if pid is not None:
        match.update({"pid": pid})

    if category is not None:
        match.update({"category": category})

    return list(db.submissions.find(match, {"_id":0}))

def clear_all_submissions():
    """
    Removes all submissions from the database.
    """

    db = api.common.get_conn()
    db.submissions.remove()

def clear_submissions(uid=None, tid=None):
    """
    Clear submissions from a given team or user.

    Args:
        uid: the user's uid to clear from.
        tid: the team's tid to clear from.
    """

    db = api.common.get_conn()

    match = {}

    if uid is not None:
        match.update({"uid": uid})
    elif tid is not None:
        match.update({"tid": tid})
    else:
        raise InternalException("You must supply either a tid or uid")

    return db.submissions.remove(match)

def invalidate_submissions(pid=None, uid=None, tid=None):
    """
    Invalidates the submissions for a given problem. Can be filtered by uid or tid.
    Passing no arguments will invalidate all submissions.

    Args:
        pid: the pid of the problem.
        uid: the user's uid that will his submissions invalidated.
        tid: the team's tid that will have their submissions invalidated.
    """

    db = api.common.get_conn()

    match = {}

    if pid is not None:
        match.update({"pid": pid})

    if uid is not None:
        match.update({"uid": uid})
    elif tid is not None:
        match.update({"tid": tid})

    db.submissions.update(match, {"correct": False})

def reevaluate_submissions_for_problem(pid):
    """
    In the case of the problem or grader being updated, this will reevaluate all submissions.
    This will NOT work for auto generated problems.

    Args:
        pid: the pid of the problem to be reevaluated.
    Returns:
        A list of affected tids.
    """

    db = api.common.get_conn()

    problem = get_problem(pid=pid)

    keys = {}
    for submission in get_submissions(pid=pid):
        key = submission["key"]
        if key not in keys:
            result = grade_problem(pid, key)
            if result["correct"] != submission["correct"]:
                keys[key] = result["correct"]
            else:
                keys[key] = None

    for key, change in keys.items():
        if change is not None:
            db.submissions.update({"key": key}, {"correct": change}, multi=True)

def get_problem(pid=None, name=None, tid=None, show_disabled=False):
    """
    Gets a single problem.

    Args:
        pid: The problem id
        name: The name of the problem
        show_disabled: Boolean indicating whether or not to show disabled problems.
    Returns:
        The problem dictionary from the database
    """

    db = api.common.get_conn()

    match = {"disabled": show_disabled}
    if pid is not None:
        match.update({'pid': pid})
    elif name is not None:
        match.update({'name': name})
    else:
        raise InternalException("Must supply pid or display name")

    if tid is not None and pid not in get_unlocked_pids(tid):
        raise InternalException("You cannot get this problem")

    db = api.common.get_conn()
    problem = db.problems.find_one(match, {"_id":0})

    if problem is None:
        raise SevereInternalException("Could not find problem! You gave " + str(match))

    return problem

def get_all_problems(category=None, show_disabled=False):
    """
    Gets all of the problems in the database.

    Args:
        category: Optional parameter to restrict which problems are returned
        show_disabled: Boolean indicating whether or not to show disabled problems.
    Returns:
        List of problems from the database
    """

    db = api.common.get_conn()

    match = {"disabled": show_disabled}
    if category is not None:
      match.update({'category': category})

    return list(db.problems.find(match, {"_id":0}).sort('score', pymongo.ASCENDING))

def get_solved_pids(tid, category=None):
    """
    Gets the solved pids for a given team.

    Args:
        tid: The team id
        category: Optional parameter to restrict which problems are returned
    Returns:
        List of solved problem ids
    """

    return [sub['pid'] for sub in get_submissions(tid=tid, category=category) if sub['correct'] == True]


def get_solved_problems(tid, category=None):
    """
    Gets the solved problems for a given team.

    Args:
        tid: The team id
        category: Optional parameter to restrict which problems are returned
    Returns:
        List of solved problem dictionaries
    """

    return [get_problem(pid) for pid in get_solved_pids(tid, category)]

def get_unlocked_pids(tid, category=None):
    """
    Gets the unlocked pids for a given team.

    Args:
        tid: The team id
        category: Optional parameter to restrict which problems are returned
    Returns:
        List of unlocked problem ids
    """

    solved = get_solved_problems(tid, category)

    unlocked = []
    for problem in get_all_problems():
        if 'weightmap' not in problem or 'threshold' not in problem:
            unlocked.append(problem['pid'])
        else:
            weightsum = sum(problem['weightmap'].get(p['pid'], 0) for p in solved)
            if weightsum >= problem['threshold']:
                unlocked.append(problem['pid'])

    return unlocked

def get_unlocked_problems(tid, category=None):
    """
    Gets the unlocked problems for a given team.

    Args:
        tid: The team id
        category: Optional parameter to restrict which problems are returned
    Returns:
        List of unlocked problem dictionaries
    """

    solved = get_solved_problems(tid)
    unlocked = [get_problem(pid) for pid in get_unlocked_pids(tid, category)]
    for problem in unlocked:
        problem['solved'] = problem in solved

    return unlocked
