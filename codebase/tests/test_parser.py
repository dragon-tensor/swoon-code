from __future__ import annotations

import unittest

from swoon.aeml import AEMLParseError, AEMLParser, AEMLTruncatedError, NextDirective, Root


class AEMLParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = AEMLParser()

    def test_parses_read_action_and_typed_structure(self) -> None:
        source = """
        <aeml turn="2" session="sess_7c1">
          <plan>1. Read the file</plan>
          <action id="b2">
            <tool>read-file</tool>
            <path root="input">app.py</path>
            <args><start_line>1</start_line><end_line>60</end_line></args>
          </action>
          <next>await_result</next>
        </aeml>
        """
        message = self.parser.parse(source)

        self.assertEqual(message.turn, 2)
        self.assertEqual(message.session, "sess_7c1")
        self.assertEqual(message.next, NextDirective.AWAIT_RESULT)
        self.assertEqual(len(message.actions), 1)
        action = message.actions[0]
        self.assertEqual(action.id, "b2")
        self.assertEqual(action.path.root, Root.INPUT)
        self.assertEqual(action.path.value, "app.py")
        self.assertEqual(action.argument("start_line").value, "1")

    def test_cdata_preserves_source_code(self) -> None:
        source = """
        <aeml turn="1" session="sess_code">
          <action id="c1">
            <tool>create-file</tool><path>page.html</path>
            <args><content><![CDATA[<p>x & y</p>
<!-- ordinary source-code comment -->]]></content></args>
            <chunk seq="1" final="true"/>
          </action>
          <next>await_result</next>
        </aeml>
        """
        message = self.parser.parse(source)

        content = message.actions[0].argument("content").value
        self.assertEqual(content, "<p>x & y</p>\n<!-- ordinary source-code comment -->")
        self.assertEqual(message.actions[0].chunk.seq, 1)
        self.assertTrue(message.actions[0].chunk.final)

    def test_missing_closing_envelope_is_classified_as_truncation(self) -> None:
        with self.assertRaises(AEMLTruncatedError) as raised:
            self.parser.parse(
                '<aeml turn="1" session="sess_x"><action id="a1"><tool>create-file</tool>'
            )
        self.assertEqual(raised.exception.code, "likely_truncated_by_message_limit")

    def test_prose_outside_envelope_is_rejected(self) -> None:
        with self.assertRaises(AEMLParseError):
            self.parser.parse(
                'Here you go: <aeml turn="1" session="sess_x"><next>proceed</next></aeml>'
            )

    def test_unknown_tag_is_rejected(self) -> None:
        with self.assertRaisesRegex(AEMLParseError, "Unknown <aeml> child"):
            self.parser.parse(
                '<aeml turn="1" session="sess_x"><magic/><next>proceed</next></aeml>'
            )

    def test_dtd_and_entity_declarations_are_rejected(self) -> None:
        source = """<!DOCTYPE aeml [<!ENTITY x "boom">]>
        <aeml turn="1" session="sess_x"><say>&x;</say><next>proceed</next></aeml>"""
        with self.assertRaisesRegex(AEMLParseError, "forbidden"):
            self.parser.parse(source)

    def test_duplicate_singleton_tag_is_rejected(self) -> None:
        with self.assertRaisesRegex(AEMLParseError, "at most once"):
            self.parser.parse(
                '<aeml turn="1" session="sess_x">'
                '<next>proceed</next><next>abort</next></aeml>'
            )

    def test_missing_session_attribute_is_rejected(self) -> None:
        with self.assertRaisesRegex(AEMLParseError, "missing attribute"):
            self.parser.parse('<aeml turn="1"><next>proceed</next></aeml>')

    def test_invalid_boolean_is_rejected(self) -> None:
        with self.assertRaisesRegex(AEMLParseError, "true or false"):
            self.parser.parse(
                '<aeml turn="1" session="sess_x"><action id="a1">'
                '<tool>delete-file</tool><path>x</path><expect_confirm>yes</expect_confirm>'
                '</action><next>await_result</next></aeml>'
            )

    def test_parser_size_limit_is_enforced(self) -> None:
        parser = AEMLParser(max_message_bytes=32)
        with self.assertRaisesRegex(AEMLParseError, "parser limit"):
            parser.parse('<aeml turn="1" session="sess_x"><next>proceed</next></aeml>')

    def test_invalid_unicode_is_a_structured_parse_error(self) -> None:
        with self.assertRaises(AEMLParseError) as raised:
            self.parser.parse(
                '<aeml turn="1" session="sess_x"><say>\ud800</say>'
                "<next>proceed</next></aeml>"
            )
        self.assertEqual(raised.exception.code, "parse_error")


if __name__ == "__main__":
    unittest.main()
