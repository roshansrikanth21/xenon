import threading
import unittest

from xenon import conversation
from xenon.conversation import ConversationLog


class ConversationLogTests(unittest.TestCase):
    def test_messages_alternate_user_and_assistant_oldest_first(self) -> None:
        log = ConversationLog()
        log.add("play daft punk", "Playing Daft Punk.", now=100)
        log.add("louder", "Volume up.", now=110)

        messages = log.messages(now=120)

        self.assertEqual(
            messages,
            [
                {"role": "user", "content": "play daft punk"},
                {"role": "assistant", "content": "Playing Daft Punk."},
                {"role": "user", "content": "louder"},
                {"role": "assistant", "content": "Volume up."},
            ],
        )

    def test_messages_returns_only_the_last_n_turns(self) -> None:
        log = ConversationLog()
        for index in range(5):
            log.add(f"q{index}", f"a{index}", now=100 + index)

        messages = log.messages(n=2, now=200)

        self.assertEqual([m["content"] for m in messages], ["q3", "a3", "q4", "a4"])

    def test_maxlen_drops_the_oldest_turns(self) -> None:
        log = ConversationLog(maxlen=3)
        for index in range(5):
            log.add(f"q{index}", f"a{index}", now=100)

        self.assertEqual(len(log), 3)
        self.assertEqual(log.messages(n=10, now=100)[0]["content"], "q2")

    def test_long_text_is_clipped(self) -> None:
        log = ConversationLog()
        log.add("x" * 1000, "y" * 1000, now=100)

        user, reply = log.messages(clip=50, now=100)

        self.assertLessEqual(len(user["content"]), 50)
        self.assertLessEqual(len(reply["content"]), 50)
        self.assertTrue(user["content"].endswith("..."))

    def test_invisible_characters_are_removed_from_user_text(self) -> None:
        log = ConversationLog()
        log.add("pl​ay it‮ again", "Okay.", now=100)

        user = log.messages(now=100)[0]["content"]

        self.assertEqual(user, "play it again")

    def test_old_turns_expire(self) -> None:
        log = ConversationLog(max_age_s=1200)
        log.add("old topic", "old answer", now=0)
        log.add("new topic", "new answer", now=1500)

        messages = log.messages(now=1600)

        self.assertEqual([m["content"] for m in messages], ["new topic", "new answer"])
        self.assertNotIn("old topic", log.as_text(now=1600))
        self.assertEqual(log.messages(now=5000), [])

    def test_private_turns_can_be_filtered(self) -> None:
        log = ConversationLog()
        log.add("my bank pin thing", "Done.", private=True, now=100)
        log.add("what time is it", "Nine.", now=101)

        everything = log.messages(now=102)
        public = log.messages(include_private=False, now=102)

        self.assertEqual(len(everything), 4)
        self.assertEqual([m["content"] for m in public], ["what time is it", "Nine."])
        self.assertNotIn("bank", log.as_text(include_private=False, now=102))

    def test_as_text_format(self) -> None:
        log = ConversationLog()
        log.add("play daft punk", "Playing Daft Punk.", kind="media", now=100)
        log.add("who is that", "Daft Punk are a French duo.", now=101)

        text = log.as_text(now=102)

        self.assertEqual(
            text,
            "User: play daft punk / Xenon: Playing Daft Punk.\n"
            "User: who is that / Xenon: Daft Punk are a French duo.",
        )

    def test_as_text_clips_and_limits_turns(self) -> None:
        log = ConversationLog()
        for index in range(5):
            log.add(f"question {index} " + "z" * 300, "answer", now=100)

        lines = log.as_text(n=2, clip=40, now=100).splitlines()

        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith("User: question 3"))
        self.assertLess(len(lines[0]), 40 + len("User:  / Xenon: answer") + 1)

    def test_last_user_clear_and_len(self) -> None:
        log = ConversationLog()
        self.assertEqual(log.last_user(now=0), "")
        log.add("first", "1", now=100)
        log.add("second", "2", now=101)

        self.assertEqual(log.last_user(now=102), "second")
        self.assertEqual(len(log), 2)

        log.clear()

        self.assertEqual(len(log), 0)
        self.assertEqual(log.messages(now=102), [])

    def test_empty_exchanges_are_ignored(self) -> None:
        log = ConversationLog()
        log.add("", "", now=100)
        log.add("   ", "​", now=100)

        self.assertEqual(len(log), 0)

    def test_concurrent_adds_and_reads_are_safe(self) -> None:
        log = ConversationLog(maxlen=50)
        errors: list[BaseException] = []

        def writer(tag: str) -> None:
            try:
                for index in range(200):
                    log.add(f"{tag}{index}", "ok")
            except BaseException as exc:  # pragma: no cover - failure path
                errors.append(exc)

        def reader() -> None:
            try:
                for _ in range(200):
                    messages = log.messages(n=10)
                    self.assertEqual(len(messages) % 2, 0)
                    log.as_text()
            except BaseException as exc:  # pragma: no cover - failure path
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(tag,)) for tag in "abcd"]
        threads += [threading.Thread(target=reader) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(errors, [])
        self.assertEqual(len(log), 50)

    def test_shared_returns_one_log(self) -> None:
        self.assertIs(conversation.shared(), conversation.shared())
        self.assertIsInstance(conversation.shared(), ConversationLog)


if __name__ == "__main__":
    unittest.main()
