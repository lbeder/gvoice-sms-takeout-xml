import dateutil.parser
import os
import phonenumbers
import re
import time
from base64 import b64encode
from bs4 import BeautifulSoup
from io import open  # adds emoji support
from pathlib import Path
from shutil import copyfileobj, move
from tempfile import NamedTemporaryFile

sms_backup_filename = "./gvoice-all.xml"
sms_backup_path = Path(sms_backup_filename)
# Clear file if it already exists
sms_backup_path.open("w").close()
print("New file will be saved to " + sms_backup_filename)


def main():
    print("Checking directory for *.html files")
    num_sms = 0
    root_dir = "."

    for subdir, dirs, files in os.walk(root_dir):
        for file in files:
            sms_filename = os.path.join(subdir, file)

            if os.path.splitext(sms_filename)[1] != ".html":
                # print(sms_filename,"- skipped")
                continue

            print("Processing " + sms_filename)

            is_group_conversation = re.match(r"(^Group Conversation)", file)

            with open(sms_filename, "r", encoding="utf8") as sms_file:
                soup = BeautifulSoup(sms_file, "html.parser")

            messages_raw = soup.find_all(class_="message")
            # Skip files with no messages
            if not len(messages_raw):
                continue

            num_sms += len(messages_raw)

            if is_group_conversation:
                participants_raw = soup.find_all(class_="participants")
                write_mms_messages(file, participants_raw, messages_raw)
            else:
                write_sms_messages(file, messages_raw)

    sms_backup_file = open(sms_backup_filename, "a")
    sms_backup_file.write("</smses>")
    sms_backup_file.close()

    write_header(sms_backup_filename, num_sms)


def write_sms_messages(file, messages_raw):
    fallback_number = 0
    title_has_number = re.search(r"(^\+[0-9]+)", Path(file).name)
    if title_has_number:
        fallback_number = title_has_number.group()

    phone_number, participant_raw = get_first_phone_number(
        messages_raw, fallback_number
    )

    # Search similarly named files for a fallback number. This is desperate and expensive, but
    # hopefully rare.
    if phone_number == 0:
        file_prefix = "-".join(Path(file).stem.split("-")[0:1])
        for fallback_file in Path.cwd().glob(f"**/{file_prefix}*.html"):
            with fallback_file.open("r", encoding="utf8") as ff:
                soup = BeautifulSoup(ff, "html.parser")
            messages_raw_ff = soup.find_all(class_="message")
            phone_number, participant_raw = get_first_phone_number(
                messages_raw_ff, 0)
            if phone_number != 0:
                break

    # Start looking in the Placed/Received files for a fallback number
    if phone_number == 0:
        file_prefix = f'{Path(file).stem.split("-")[0]}- '
        for fallback_file in Path.cwd().glob(f"**/{file_prefix}*.html"):
            with fallback_file.open("r", encoding="utf8") as ff:
                soup = BeautifulSoup(ff, "html.parser")
            vcards = soup.find_all(class_="contributor vcard")
            phone_number_ff = 0
            for vcard in vcards:
                phone_number_ff = vcard.a["href"][4:]
            phone_number, participant_raw = get_first_phone_number(
                [], phone_number_ff)
            if phone_number != 0:
                break

    sms_values = {"phone": phone_number}

    sms_backup_file = open(sms_backup_filename, "a", encoding="utf8")
    for message in messages_raw:
        # Check if message has an image in it and treat as mms if so
        if message.find_all("img"):
            write_mms_messages(file, [[participant_raw]], [message])
            continue

        sms_values["type"] = get_message_type(message)
        sms_values["message"] = get_message_text(message)
        sms_values["time"] = get_time_unix(message)
        sms_text = (
            '<sms protocol="0" address="%(phone)s" '
            'date="%(time)s" type="%(type)s" '
            'subject="null" body="%(message)s" '
            'toa="null" sc_toa="null" service_center="null" '
            'read="1" status="1" locked="0" /> \n' % sms_values
        )
        sms_backup_file.write(sms_text)

    sms_backup_file.close()


def write_mms_messages(file, participants_raw, messages_raw):
    sms_backup_file = open(sms_backup_filename, "a", encoding="utf8")

    participants = get_participant_phone_numbers(participants_raw)
    participants_text = "~".join(participants)

    for message in messages_raw:
        # Sometimes the sender tel field is blank. Try to guess the sender from the participants.
        sender = get_mms_sender(message, participants)
        sent_by_me = sender not in participants

        # Handle images
        images = message.find_all("img")
        image_parts = ""
        if images:
            for image in images:
                # I have only encountered jpg and gif, but I have read that GV can ecxport png
                supported_types = ["jpg", "png", "gif"]
                image_filename = image["src"]
                original_image_filename = image_filename
                # Each image found should only match a single file
                image_path = list(Path.cwd().glob(f"**/*{image_filename}"))

                if len(image_path) == 0:
                    # Sometimes they just forget the extension
                    for supported_type in supported_types:
                        image_path = list(
                            Path.cwd().glob(
                                f"**/*{image_filename}.{supported_type}")
                        )
                        if len(image_path) == 1:
                            break

                if len(image_path) == 0:
                    # Sometimes the first word doesn't match (eg it is a phone number instead of a
                    # contact name) so try again without the first word
                    image_filename = "-".join(
                        original_image_filename.split("-")[1:])
                    image_path = list(Path.cwd().glob(
                        f"**/*{image_filename}*"))

                if len(image_path) == 0:
                    # Sometimes the image filename matches the message filename instead of the
                    # filename in the HTML. And sometimes the message filenames are repeated, eg
                    # filefoo(0).html, filefoo(1).html, etc., but the image filename matches just
                    # the base ("filefoo" in this example).
                    image_filenames = [Path(file).stem, Path(
                        file).stem.split("(")[0]]
                    for image_filename in image_filenames:
                        # Have to guess at the file extension in this case
                        for supported_type in supported_types:
                            image_path = list(
                                Path.cwd().glob(
                                    f"**/*{image_filename}*.{supported_type}"
                                )
                            )
                            # Sometimes there's extra cruft in the filename in the HTML. So try to
                            # match a subset of it.
                            if len(image_path) > 1:
                                for ip in image_path:
                                    if ip.stem in original_image_filename:
                                        image_path = [ip]
                                        break

                            if len(image_path) == 1:
                                break
                        if len(image_path) == 1:
                            break

                assert (
                    len(image_path) != 0
                ), f"No matching images found. File name: {original_image_filename}"
                assert (
                    len(image_path) == 1
                ), f"Multiple potential matching images found. Images: {[x for x in image_path]!r}"

                image_path = image_path[0]
                image_type = image_path.suffix[1:]
                image_type = "jpeg" if image_type == "jpg" else image_type

                with image_path.open("rb") as fb:
                    image_bytes = fb.read()
                byte_string = f"{b64encode(image_bytes)}"

                image_parts += (
                    f'    <part seq="0" ct="image/{image_type}" name="{image_path.name}" '
                    f'chset="null" cd="null" fn="null" cid="&lt;{image_path.name}&gt;" '
                    f'cl="{image_path.name}" ctt_s="null" ctt_t="null" text="null" '
                    f'data="{byte_string[2:-1]}" />\n'
                )

        message_text = get_message_text(message)
        time = get_time_unix(message)
        participants_xml = ""
        msg_box = 2 if sent_by_me else 1
        m_type = 128 if sent_by_me else 132
        for participant in participants:
            participant_is_sender = participant == sender or (
                sent_by_me and participant == "Me"
            )
            participant_values = {
                "number": participant,
                "code": 137 if participant_is_sender else 151,
            }
            participants_xml += (
                '    <addr address="%(number)s" charset="106" type="%(code)s"/> \n'
                % participant_values
            )

        mms_text = (
            f'<mms address="{participants_text}" ct_t="application/vnd.wap.multipart.related" '
            f'date="{time}" m_type="{m_type}" msg_box="{msg_box}" read="1" '
            'rr="129" seen="1" sub_id="-1" text_only="1"> \n'
            "  <parts> \n"
            f'    <part ct="text/plain" seq="0" text="{message_text}"/> \n'
            + image_parts
            + "  </parts> \n"
            "  <addrs> \n"
            f"{participants_xml}"
            "  </addrs> \n"
            "</mms> \n"
        )

        sms_backup_file.write(mms_text)

    sms_backup_file.close()


def get_message_type(message):  # author_raw = messages_raw[i].cite
    author_raw = message.cite
    if not author_raw.span:
        return 2
    else:
        return 1

    return 0


def escape(message):
    return message.replace("<br/>", "&#10;").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\"", "&quot;").replace("'", "&apos;")


def get_message_text(message):
    # Attempt to properly translate newlines. Might want to translate other HTML here, too.
    # This feels very hacky, but couldn't come up with something better.
    return escape(str(message.find("q")).strip()[3:-4])


def get_mms_sender(message, participants):
    number_text = message.cite.a["href"][4:]
    if number_text != "":
        number = format_number(phonenumbers.parse(number_text, None))
    else:
        assert (
            len(participants) == 1
        ), "Unable to determine sender in mms with multiple participants"
        number = participants[0]
    return number


def get_first_phone_number(messages, fallback_number):
    # handle group messages
    for author_raw in messages:
        if not author_raw.span:
            continue

        sender_data = author_raw.cite
        # Skip if first number is Me
        if sender_data.text == "Me":
            continue
        phonenumber_text = sender_data.a["href"][4:]
        # Sometimes the first entry is missing a phone number
        if phonenumber_text == "":
            continue

        try:
            phone_number = phonenumbers.parse(phonenumber_text, None)
        except phonenumbers.phonenumberutil.NumberParseException:
            return phonenumber_text, sender_data

        # sender_data can be used as participant for mms
        return format_number(phone_number), sender_data

    # fallback case, use number from filename
    if fallback_number != 0 and len(fallback_number) >= 7:
        fallback_number = format_number(
            phonenumbers.parse(fallback_number, None))
    # Create dummy participant
    sender_data = BeautifulSoup(
        f'<cite class="sender vcard"><a class="tel" href="tel:{fallback_number}"><abbr class="fn" '
        'title="">Foo</abbr></a></cite>',
        features="html.parser",
    )
    return fallback_number, sender_data


def get_participant_phone_numbers(participants_raw):
    participants = []

    for participant_set in participants_raw:
        for participant in participant_set:
            if not hasattr(participant, "a"):
                continue

            phone_number_text = participant.a["href"][4:]
            assert (
                phone_number_text != "" and phone_number_text != "0"
            ), "Could not find participant phone number. Usually caused by empty tel field."
            try:
                participants.append(
                    format_number(phonenumbers.parse(phone_number_text, None))
                )
            except phonenumbers.phonenumberutil.NumberParseException:
                participants.append(phone_number_text)

    return participants


def format_number(phone_number):
    return phonenumbers.format_number(phone_number, phonenumbers.PhoneNumberFormat.E164)


def get_time_unix(message):
    time_raw = message.find(class_="dt")
    ymdhms = time_raw["title"]
    time_obj = dateutil.parser.isoparse(ymdhms)
    mstime = time.mktime(time_obj.timetuple()) * 1000
    return int(mstime)


def write_header(filename, numsms):
    # Prepend header in memory efficient manner since the output file can be huge
    with NamedTemporaryFile(dir=Path.cwd(), delete=False) as backup_temp:
        backup_temp.write(
            b"<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>\n")
        backup_temp.write(b"<!--Converted from GV Takeout data -->\n")
        backup_temp.write(
            bytes(f'<smses count="{str(numsms)}">\n', encoding="utf8"))
        with open(filename, "rb") as backup_file:
            copyfileobj(backup_file, backup_temp)
    # Overwrite output file with temp file
    move(backup_temp.name, filename)


main()
