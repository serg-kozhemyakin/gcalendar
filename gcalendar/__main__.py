#!/usr/bin/env python3
# gcalendar is a tool to read Google Calendar events from your terminal.

# Copyright (C) 2020  Gobinath

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import argparse
import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from os.path import join
from pathlib import Path

import dateutil
from dateutil.relativedelta import relativedelta
from googleapiclient.errors import HttpError
from httplib2 import HttpLib2Error
from oauth2client import client
from oauth2client import clientsecrets

from gcalendar import DEFAULT_CLIENT_ID, DEFAULT_CLIENT_SECRET, TOKEN_STORAGE_VERSION, VERSION
from gcalendar.gcalendar import GCalendar

# the home folder
HOME_DIRECTORY = os.environ.get('HOME') or os.path.expanduser('~')

# ~/.config/gcalendar folder
CONFIG_DIRECTORY = os.path.join(os.environ.get(
    'XDG_CONFIG_HOME') or os.path.join(HOME_DIRECTORY, '.config'), 'gcalendar')

# ~/.cache/gcalendar folder
CACHE_DIRECTORY = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.join(HOME_DIRECTORY, ".cache"),
                               "gcalendar",
                               )

TOKEN_FILE_SUFFIX = "_" + TOKEN_STORAGE_VERSION + ".dat"


def validate_account_id(account_id):
    """
    Validate the argparse argument --account
    """
    account = str(account_id)
    if not account.isalnum():
        raise argparse.ArgumentTypeError("%s is not an alphanumeric id" % account)
    return account


def validate_since(date):
    """
    Validate the argparse argument --since
    """
    try:
        return datetime.strptime(date, "%Y-%m-%d").astimezone()
    except ValueError:
        raise argparse.ArgumentTypeError(date + " is not in %Y-%m-%d format")


def delete_if_exist(file_path):
    try:
        os.remove(file_path)
    except OSError:
        pass


def list_accounts():
    accounts = list()
    for file in os.listdir(CONFIG_DIRECTORY):
        if os.path.isfile(join(CONFIG_DIRECTORY, file)) and file.endswith(TOKEN_FILE_SUFFIX):
            accounts.append(file.replace(TOKEN_FILE_SUFFIX, ""))
    return accounts


def reset_account(account_id, storage_path, cache_path):
    if os.path.exists(storage_path):
        delete_if_exist(storage_path)
        delete_if_exist(cache_path)
        if os.path.exists(storage_path):
            return "Failed to reset %s" % account_id
        else:
            return "Successfully reset %s" % account_id
    else:
        return "Account %s does not exist" % account_id


def handle_error(error, message, output_type, debug_mode):
    if output_type == "txt":
        print("\033[91m" + message + "\033[0m")
    elif output_type == "json":
        print('{"error": "%s"}' % message)
    if debug_mode:
        raise error


def print_status(status, output_type):
    if output_type == "txt":
        print(status)
    elif output_type == "json":
        print('{"status": "%s"}' % status)


def print_list(obj_list, output_type):
    if output_type == "txt":
        for acc in obj_list:
            print(acc)
    elif output_type == "json":
        print(json.dumps(obj_list))


def format_event(event, event_format):
    pattern = r"{(?!{)(\w+)}(?!})"
    fields = re.findall(pattern, event_format)
    for field in fields:
        if field not in event:
            return f"Invalid field name: '{field}'"
    try:
        return event_format.format(**event)
    except ValueError as e:
        return f"{e}: {event_format}"


def print_events(events, output_type, event_format):
    if output_type == "txt":
        for event in events:
            print(format_event(event, event_format))
    elif output_type == "json":
        print(json.dumps(events))


def handle_exception(client_id, client_secret, account_id, storage_path, output, debug, function):
    failed = False
    try:
        g_calendar = GCalendar(client_id, client_secret, account_id, storage_path)
        return failed, function(g_calendar)

    except clientsecrets.InvalidClientSecretsError as ex:
        handle_error(ex, "Invalid Client Secrets", output, debug)
        failed = True

    except client.AccessTokenRefreshError as ex:
        handle_error(ex, "Failed to refresh access token", output, debug)
        failed = True

    except HttpLib2Error as ex:
        if "Unable to find the server at" in str(ex):
            msg = "Unable to find the Google Calendar server. Please check your connection."
        else:
            msg = "Failed to connect Google Calendar"
        handle_error(ex, msg, output, debug)
        failed = True

    except HttpError as ex:
        if "Too Many Requests" in str(ex):
            msg = "You have reached your request quota limit. Please try gcalendar after a few minutes."
        else:
            msg = "Failed to connect Google Calendar"

        handle_error(ex, msg, output, debug)
        failed = True

    except BaseException as ex:
        handle_error(ex, "Failed to connect Google Calendar", output, debug)
        failed = True
    return failed, None


def interval_to_seconds(interval, negative=False):
    seconds_per_unit = {"m": 60, "h": 3600, "d": 86400}
    pattern = r"\s*([-+]?)(\d+)\s*(?:(h(?:our)?|m(?:inute)?|d(?:ay)?)s?)\s*"
    time_intervals = re.findall(pattern, interval)
    seconds = 0
    for interval in time_intervals:
        n = int(interval[1])
        if negative and interval[0] != "+":
            n = -n
        seconds += n * seconds_per_unit[(interval[2] or "m")[0]]
    return seconds


def cache_path(account_id):
    return Path(CACHE_DIRECTORY) / account_id


def read_cached_events(account_id, cache_ttl):
    cache = cache_path(account_id)
    events = []
    if cache.is_file() and time.time() < (
            cache.stat().st_mtime + interval_to_seconds(cache_ttl)
    ):
        with open(cache, "r") as f:
            events = json.load(f)
    return events


def cache_events(account_id, events):
    cache = cache_path(account_id)
    cache.parent.mkdir(parents=True, exist_ok=True)
    with open(cache, "w") as f:
        json.dump(events, f)


def notify_events(events, notifier, notify_before, event_format):
    intervals = [
        interval_to_seconds(interval, negative=True) for interval in notify_before
    ]
    intervals.sort()
    for event in events:
        event_desc = format_event(event, event_format)
        event_start = dateutil.parser.parse(
            f"{event['start_date']} {event['start_time']}"
        )
        event_start_00 = event_start.replace(second=00).timestamp()
        event_start_59 = event_start.replace(second=59).timestamp()
        for interval in intervals:
            if (
                    (event_start_00 + interval)
                    <= time.time()
                    <= (event_start_59 + interval)
            ):
                subprocess.run([notifier, event_desc])
                break


def process_events(events, args):
    if args.notify:
        event_format = (
                args.event_format
                or "{start_date} {start_time} - {end_date} {end_time}\n{summary}\n{hangoutLink}"
        )
        notify_events(events, args.notifier, args.notify_before, event_format)
    else:
        event_format = (
                args.event_format
                or "{start_date}:{start_time} - {end_date}:{end_time}\t{summary}\t{location}\t{status}"
        )
        print_events(events, args.output, event_format)


def process_request(account_ids, args):
    client_id = args.client_id
    client_secret = args.client_secret
    if not client_id or not client_secret:
        client_id = DEFAULT_CLIENT_ID
        client_secret = DEFAULT_CLIENT_SECRET

    if args.list_accounts:
        # --list-accounts
        print_list(list_accounts(), args.output)
        return 0
    elif args.reset:
        # --reset
        for account_id in account_ids:
            storage_path = join(CONFIG_DIRECTORY, account_id + TOKEN_FILE_SUFFIX)
            status = reset_account(account_id, storage_path, cache_path(account_id).as_posix())
            print_status(status, args.output)
        return 0

    elif args.status:
        # --status
        for account_id in account_ids:
            storage_path = join(CONFIG_DIRECTORY, account_id + TOKEN_FILE_SUFFIX)
            if os.path.exists(storage_path):
                if GCalendar.is_authorized(storage_path):
                    status = "Authorized"
                else:
                    status = "Token Expired"
            else:
                status = "Not authenticated"
            print_status(status, args.output)
        return 0

    elif args.list_calendars:
        # --list-calendars
        calendars = []
        for account_id in account_ids:
            storage_path = join(CONFIG_DIRECTORY, account_id + TOKEN_FILE_SUFFIX)
            failed, result = handle_exception(client_id, client_secret, account_id, storage_path, args.output,
                                              args.debug,
                                              lambda cal: cal.list_calendars())
            if failed:
                return -1
            else:
                calendars.extend(result)
        print_list(calendars, args.output)
    else:
        # List events
        no_of_days = int(args.no_of_days)
        selected_calendars = [x.lower() for x in args.calendar]
        since = args.since
        current_time = datetime.now(timezone.utc).astimezone()
        time_zone = current_time.tzinfo
        if since is None:
            since = current_time
        start_time = str(since.isoformat())
        end_time = str((since + relativedelta(days=no_of_days)).isoformat())
        events = []
        for account_id in account_ids:
            if not args.cache or not (
                    result := read_cached_events(account_id, args.cache_ttl)
            ):
                storage_path = join(CONFIG_DIRECTORY, account_id + TOKEN_FILE_SUFFIX)
                failed, result = handle_exception(client_id, client_secret, account_id, storage_path, args.output,
                                                  args.debug,
                                                  lambda cal: cal.list_events(selected_calendars, start_time, end_time,
                                                                              time_zone))
                if failed:
                    return -1
                if args.cache:
                    cache_events(account_id, result)
            events.extend(result)
        events = sorted(events, key=lambda event: event["start_date"] + event["start_time"])
        process_events(events, args)


def main():
    """
    Retrieve Google Calendar events.
    """
    parser = argparse.ArgumentParser(prog='gcalendar', description="Read your Google Calendar events from terminal.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--list-calendars", action="store_true", help="list all calendars from the Google account")
    group.add_argument("--list-accounts", action="store_true", help="list the id of gcalendar accounts")
    group.add_argument("--status", action="store_true", help="print the status of the gcalendar account")
    group.add_argument("--reset", action="store_true", help="reset the account")
    group.add_argument("--notify", action="store_true", help="notify about upcoming events")
    parser.add_argument("--calendar", type=str, default=["*"], nargs="*", help="calendars to list events from")
    parser.add_argument("--since", type=validate_since, help="number of days to include")
    parser.add_argument("--no-of-days", type=str, default="7", help="number of days to include")
    parser.add_argument("--account", type=validate_account_id, default=["default"], nargs="*",
                        help="an alphanumeric name to uniquely identify the account")
    parser.add_argument("--output", choices=["txt", "json"], default="txt", help="output format")
    parser.add_argument("--client-id", type=str, help="the Google client id")
    parser.add_argument("--client-secret", type=str, help="the Google client secret")
    parser.add_argument("--cache", dest="cache", action="store_true", default=True,
                        help="cache the calendar events. enabled by default")
    parser.add_argument("--no-cache", dest="cache", action="store_false", help="skip the calendar events cache")
    parser.add_argument("--cache-ttl", type=str, default="30m", help="ttl of cache for the calendar events")
    parser.add_argument("--notifier", type=str, default="notify-send",
                        help="notifier to use for upcoming events notifications")
    parser.add_argument("--notify-before", nargs="+", type=str, default=["2m", "1m", "+0m"],
                        help="time to notify before event")
    parser.add_argument("--event-format", type=str, help="format of event for notification")
    parser.add_argument('--version', action='version', version='%(prog)s ' + VERSION)
    parser.add_argument("--debug", action="store_true", help="run gcalendar in debug mode")
    args = parser.parse_args()

    # Create the config folder if not exists
    if not os.path.exists(CONFIG_DIRECTORY):
        os.mkdir(CONFIG_DIRECTORY)

    return process_request(args.account, args)


if __name__ == "__main__":
    main()
