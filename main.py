import inspect
import logging
import os
import socket
import sys
from datetime import datetime
from typing import Tuple, Optional, Callable, Union, NewType, List

import pytz
import requests
import urllib3
from bs4 import BeautifulSoup, Tag
from telegram import Bot

WOOG_TEMPERATURE_URL = "https://www.hamburg.de/clp/hu/lombardsbruecke/clp1/"
# noinspection HttpUrlsUsage
# cluster internal communication
BACKEND_URL = os.getenv("BACKEND_URL") or "http://api:80"
BACKEND_PATH = os.getenv("BACKEND_PATH") or "lake/{}/temperature"
UUID = os.getenv("ALSTER_UUID")
API_KEY = os.getenv("API_KEY")

WATER_INFORMATION = NewType("WaterInformation", Tuple[str, float])


def create_logger(name: str, level: int = logging.DEBUG) -> logging.Logger:
    logger = logging.Logger(name)
    ch = logging.StreamHandler(sys.stdout)

    formatting = "[{}] %(asctime)s\t%(levelname)s\t%(module)s.%(funcName)s#%(lineno)d | %(message)s".format(name)
    formatter = logging.Formatter(formatting)
    ch.setFormatter(formatter)

    logger.addHandler(ch)
    logger.setLevel(level)

    return logger


def send_telegram_alert(message: str, token: str, chatlist: List[str]):
    logger = create_logger(inspect.currentframe().f_code.co_name)
    if not token:
        logger.error("TOKEN not defined in environment, skip sending telegram message")
        return

    if not chatlist:
        logger.error("chatlist is empty (env var: TELEGRAM_CHATLIST)")

    for user in chatlist:
        Bot(token=token).send_message(chat_id=user, text=f"Error while executing: {message}")


def get_website() -> Tuple[str, bool]:
    logger = create_logger(inspect.currentframe().f_code.co_name)
    url = WOOG_TEMPERATURE_URL

    logger.debug(f"Requesting {url}")
    response = requests.get(url)

    content = response.content.decode("utf-8")
    logger.debug(content)

    return content, True


def parse_website_xml(xml: str) -> BeautifulSoup:
    return BeautifulSoup(xml, "html.parser")


def extract_table_row(html: BeautifulSoup) -> Tuple[Optional[Tag], bool]:
    logger = create_logger(inspect.currentframe().f_code.co_name)

    table = html.find("table")
    if not table:
        logger.error(f"table not found in html {html}")
        return None, "410 - Gone" not in html

    rows = table.find_all("tr")
    if not rows or len(rows) < 5:
        logger.error(f"tr not found or len(rows) < 5 in {table}")
        return None, True

    try:
        for row in rows:
            columns = row.find_all("td")
            if columns and "Wassertemperatur" in columns[1].text:
                return row, True
    except IndexError:
        pass

    logger.error("Couldn't find a column for 'Wassertemperatur'")
    return None, True


def get_tag_text_from_xml(xml: Union[BeautifulSoup, Tag], name: str, conversion: Callable) -> Optional:
    tag = xml.find(name)

    if not tag:
        return None

    return conversion(tag.text)


def get_water_information(soup: BeautifulSoup) -> Optional[WATER_INFORMATION]:
    logger = create_logger(inspect.currentframe().f_code.co_name)
    columns = soup.find_all("td")
    if len(columns) < 4:
        logger.error("len(columns) < 4")
        return None

    time = datetime.strptime(columns[0].text.strip(), "%d.%m.%Y %H:%M")
    local = pytz.timezone("Europe/Berlin")
    time = local.localize(time)
    iso_time = time.astimezone(pytz.utc).isoformat()

    temperature = float(columns[2].text.strip())

    # noinspection PyTypeChecker
    # at this point pycharm doesn't think that the return type can be optional despite the many empty returns beforehand
    return iso_time, temperature


def send_data_to_backend(water_information: WATER_INFORMATION) -> Tuple[
    Optional[requests.Response], str]:
    logger = create_logger(inspect.currentframe().f_code.co_name)
    path = BACKEND_PATH.format(UUID)
    url = "/".join([BACKEND_URL, path])

    water_timestamp, water_temperature = water_information
    if water_temperature <= 0:
        return None, "water_temperature is <= 0, please approve this manually."

    headers = {"Authorization": f"Bearer {API_KEY}"}
    data = {"temperature": water_temperature, "time": water_timestamp}
    logger.debug(f"Send {data} to {url}")

    try:
        response = requests.put(url, json=data, headers=headers)
        logger.debug(f"success: {response.ok} | content: {response.content}")
    except (requests.exceptions.ConnectionError, socket.gaierror, urllib3.exceptions.MaxRetryError):
        logger.exception(f"Error while connecting to backend ({url})", exc_info=True)
        return None, url

    return response, url


def main() -> Tuple[bool, str, bool]:
    logger = create_logger(inspect.currentframe().f_code.co_name)
    content, success = get_website()
    if not success:
        message = f"Couldn't retrieve website: {content}"
        logger.error(message)
        return False, message, True

    soup = parse_website_xml(content)
    temperature_row, send_alert = extract_table_row(soup)
    if not temperature_row:
        message = "Couldn't find a row with 'Wassertemperatur' as a description"
        logger.error(message)
        return False, message, send_alert

    water_information = get_water_information(temperature_row)

    if not water_information:
        message = f"Couldn't retrieve water information from {soup}"
        logger.error(message)
        return False, message, True

    response, generated_backend_url = send_data_to_backend(water_information)

    if not response or not response.ok:
        message = f"Failed to put data ({water_information}) to backend: {generated_backend_url}\n{response.content}"
        logger.error(message)
        return False, message, True

    return True, "", True


root_logger = create_logger("__main__")

if not UUID:
    root_logger.error("ALSTER_UUID not defined in environment")
elif not API_KEY:
    root_logger.error("API_KEY not defined in environment")
else:
    success, message, send_message = main()
    if not success:
        root_logger.error(f"Something went wrong ({message})")
        if send_message:
            token = os.getenv("TOKEN")
            chatlist = os.getenv("TELEGRAM_CHATLIST") or "139656428"
            send_telegram_alert(message, token=token, chatlist=chatlist.split(","))
        sys.exit(1)
