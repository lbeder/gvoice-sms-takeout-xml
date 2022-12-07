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

            if len(messages_raw):
                num_sms += len(messages_raw)

                if is_group_conversation:
                    participants_raw = soup.find_all(class_="participants")
                    write_mms_messages(file, participants_raw, messages_raw)
                else:
                    write_sms_messages(file, messages_raw)

            call_log_messages_raw = soup.find_all(class_="haudio")
            if len(call_log_messages_raw):
                num_sms += len(call_log_messages_raw)

                write_sms_messages(file, call_log_messages_raw)

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
        if message.find_all("img") or message.find_all("a", class_='video') or message.find_all("audio"):
            write_mms_messages(file, [[participant_raw]], [message])
            continue

        sms_values["type"] = get_message_type(message)
        sms_values["message"] = get_message_text(message)
        sms_values["time"] = get_message_time_unix(message)
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

        # Handle videos
        videos = message.find_all("a", class_='video')
        video_parts = ""
        if videos:
            for video in videos:
                supported_types = ["mp4", "3gp"]
                video_filename = video["href"]
                original_video_filename = video_filename
                # Each video found should only match a single file
                video_path = list(Path.cwd().glob(f"**/*{video_filename}"))

                if len(video_path) == 0:
                    # Sometimes they just forget the extension
                    for supported_type in supported_types:
                        video_path = list(
                            Path.cwd().glob(
                                f"**/*{video_filename}.{supported_type}")
                        )
                        if len(video_path) == 1:
                            break

                if len(video_path) == 0:
                    # Sometimes the first word doesn't match (eg it is a phone number instead of a
                    # contact name) so try again without the first word
                    video_filename = "-".join(
                        original_video_filename.split("-")[1:])
                    video_path = list(Path.cwd().glob(
                        f"**/*{video_filename}*"))

                if len(video_path) == 0:
                    # Sometimes the video filename matches the message filename instead of the
                    # filename in the HTML. And sometimes the message filenames are repeated, eg
                    # filefoo(0).html, filefoo(1).html, etc., but the video filename matches just
                    # the base ("filefoo" in this example).
                    video_filenames = [Path(file).stem, Path(
                        file).stem.split("(")[0]]
                    for video_filename in video_filenames:
                        # Have to guess at the file extension in this case
                        for supported_type in supported_types:
                            video_path = list(
                                Path.cwd().glob(
                                    f"**/*{video_filename}*.{supported_type}"
                                )
                            )
                            # Sometimes there's extra cruft in the filename in the HTML. So try to
                            # match a subset of it.
                            if len(video_path) > 1:
                                for ip in video_path:
                                    if ip.stem in original_video_filename:
                                        video_path = [ip]
                                        break

                            if len(video_path) == 1:
                                break
                        if len(video_path) == 1:
                            break

                assert (
                    len(video_path) != 0
                ), f"No matching videos found. File name: {original_video_filename}"
                assert (
                    len(video_path) == 1
                ), f"Multiple potential matching videos found. Videos: {[x for x in video_path]!r}"

                video_path = video_path[0]
                video_type = video_path.suffix[1:]
                video_type = "3gpp" if video_type == "3pg" else video_type

                with video_path.open("rb") as fb:
                    video_bytes = fb.read()
                byte_string = f"{b64encode(video_bytes)}"

                video_parts += (
                    f'    <part seq="0" ct="video/{video_type}" name="{video_path.name}" '
                    f'chset="null" cd="null" fn="null" cid="&lt;{video_path.name}&gt;" '
                    f'cl="{video_path.name}" ctt_s="null" ctt_t="null" text="null" '
                    f'data="{byte_string[2:-1]}" />\n'
                )

        # Handle audios
        audios = message.find_all("audio")
        audio_parts = ""
        if audios:
            for audio in audios:
                supported_types = ["mp3", "amr"]
                audio_filename = audio["src"]
                original_audio_filename = audio_filename
                # Each audio found should only match a single file
                audio_path = list(Path.cwd().glob(f"**/*{audio_filename}"))

                if len(audio_path) == 0:
                    # Sometimes they just forget the extension
                    for supported_type in supported_types:
                        audio_path = list(
                            Path.cwd().glob(
                                f"**/*{audio_filename}.{supported_type}")
                        )
                        if len(audio_path) == 1:
                            break

                if len(audio_path) == 0:
                    # Sometimes the first word doesn't match (eg it is a phone number instead of a
                    # contact name) so try again without the first word
                    audio_filename = "-".join(
                        original_audio_filename.split("-")[1:])
                    audio_path = list(Path.cwd().glob(
                        f"**/*{audio_filename}*"))

                if len(audio_path) == 0:
                    # Sometimes the audio filename matches the message filename instead of the
                    # filename in the HTML. And sometimes the message filenames are repeated, eg
                    # filefoo(0).html, filefoo(1).html, etc., but the audio filename matches just
                    # the base ("filefoo" in this example).
                    audio_filenames = [Path(file).stem, Path(
                        file).stem.split("(")[0]]
                    for audio_filename in audio_filenames:
                        # Have to guess at the file extension in this case
                        for supported_type in supported_types:
                            audio_path = list(
                                Path.cwd().glob(
                                    f"**/*{audio_filename}*.{supported_type}"
                                )
                            )
                            # Sometimes there's extra cruft in the filename in the HTML. So try to
                            # match a subset of it.
                            if len(audio_path) > 1:
                                for ip in audio_path:
                                    if ip.stem in original_audio_filename:
                                        audio_path = [ip]
                                        break

                            if len(audio_path) == 1:
                                break
                        if len(audio_path) == 1:
                            break

                assert (
                    len(audio_path) != 0
                ), f"No matching audios found. File name: {original_audio_filename}"
                assert (
                    len(audio_path) == 1
                ), f"Multiple potential matching audios found. Audios: {[x for x in audio_path]!r}"

                audio_path = audio_path[0]
                audio_type = audio_path.suffix[1:]
                audio_type = "mpeg" if audio_type == "mp3" else audio_type

                with audio_path.open("rb") as fb:
                    audio_bytes = fb.read()
                byte_string = f"{b64encode(audio_bytes)}"

                audio_parts += (
                    f'    <part seq="0" ct="audio/{audio_type}" name="{audio_path.name}" '
                    f'chset="null" cd="null" fn="null" cid="&lt;{audio_path.name}&gt;" '
                    f'cl="{audio_path.name}" ctt_s="null" ctt_t="null" text="null" '
                    f'data="{byte_string[2:-1]}" />\n'
                )

        message_text = get_message_text(message)
        time = get_message_time_unix(message)
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
            + video_parts
            + audio_parts
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
    if not author_raw:
        return 1  # Someone else

    if not author_raw.span:
        return 2  # Me
    else:
        return 1  # Someone else


def get_message_text(message):
    return escape(str(message.find("q")).strip()[3:-4])


def get_call_log_text(message):
    return escape(message.find(class_="fn").text)


def get_message_time_unix(message):
    time_raw = message.find(class_="dt")
    if not time_raw:
        # Try call log format
        time_raw = message.find(class_="published")

    ymdhms = time_raw["title"]
    time_obj = dateutil.parser.isoparse(ymdhms)
    mstime = time.mktime(time_obj.timetuple()) * 1000
    return int(mstime)


def get_mms_sender(message, participants):
    if message.cite:
        sender = message.cite
    else:
        sender = message.find(class_="contributor")

    number_text = sender.a["href"][4:]

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
        if not author_raw:
            continue

        contributor = author_raw.find(class_="contributor")
        if contributor:
            sender_data = contributor
        elif author_raw.span:
            sender_data = author_raw.cite

            # Skip if first number is Me
            if sender_data.text == "Me":
                continue
        else:
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
            if phone_number_text == "" or phone_number_text == "0":
                phone_number_text = " +00000000000"

            try:
                participants.append(
                    format_number(phonenumbers.parse(phone_number_text, None))
                )
            except phonenumbers.phonenumberutil.NumberParseException:
                participants.append(phone_number_text)

    return participants


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


def format_number(phone_number):
    return phonenumbers.format_number(phone_number, phonenumbers.PhoneNumberFormat.E164)


def escape(message):
    return message.replace("<br/>", "&#10;").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\"", "&quot;").replace("'", "&apos;")


main()
