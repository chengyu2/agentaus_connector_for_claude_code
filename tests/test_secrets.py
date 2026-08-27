"""Files the bridge must never read, however a search happened to match them.

These reads do not reach Claude Code's permission prompts. That is a deliberate design
choice and it makes this list the only thing standing between a search term that happens
to appear in a key file and that key landing in a transcript.

Found by building a directory of decoys and searching it rather than by reading the code:
matching the whole filename let through `credentials.json`, which is what Google
service-account keys are called, and `id_rsa.bak`, which is a private key with four
characters after it.
"""

import unittest

from agentaus_bridge.tools import _is_secret


class MustNeverBeRead(unittest.TestCase):
    def test_environment_files_in_any_variant(self):
        for name in (".env", ".env.local", ".env.production", ".env.pdf"):
            self.assertTrue(_is_secret(name), name)

    def test_private_keys_by_extension(self):
        for name in ("server.pem", "tls.key", "bundle.p12", "a.pfx", "x.keystore"):
            self.assertTrue(_is_secret(name), name)

    def test_ssh_keys_however_they_are_suffixed(self):
        """`id_rsa.bak` is the same key as `id_rsa`. Whole-name matching missed it."""
        for name in ("id_rsa", "id_rsa.bak", "id_rsa.old", "id_ed25519.key", "id_ecdsa"):
            self.assertTrue(_is_secret(name), name)

    def test_cloud_credentials_which_are_json_not_extensionless(self):
        for name in ("credentials.json", "credentials.yml", "secrets.yaml",
                     "service_account.json", "api_key.txt"):
            self.assertTrue(_is_secret(name), name)

    def test_the_originals_still_match(self):
        for name in (".netrc", ".htpasswd", "credentials"):
            self.assertTrue(_is_secret(name), name)


class MustStillBeReadable(unittest.TestCase):
    """Over-blocking is a real cost, not a safe default.

    In a tender corpus "credentials" means professional qualifications, and a capability
    statement is exactly the document these searches exist to find. Blocking a word that
    means two things would quietly hide the evidence someone is looking for, so the
    extension decides rather than the word.
    """

    def test_a_capability_statement_is_not_a_secret(self):
        for name in ("Credentials.pdf", "Trellis Data Credentials.pdf",
                     "credentials.docx", "credentials.pptx"):
            self.assertFalse(_is_secret(name), name)

    def test_documentation_about_secrets_is_not_a_secret(self):
        for name in ("secrets_policy.docx", "api_keys_guide.md", "environment.md",
                     "keychain_notes.md"):
            self.assertFalse(_is_secret(name), name)

    def test_ordinary_files(self):
        for name in ("README.md", "config.py", "report.pdf", "server.py"):
            self.assertFalse(_is_secret(name), name)


class Enumeration(unittest.TestCase):
    def test_secrets_never_appear_in_a_candidate_list(self):
        import os
        import tempfile

        from agentaus_bridge import tools

        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, ".git"))
            for name in (".env", "private.pem", "id_rsa", "credentials.json",
                         "notes.md", "Credentials.pdf"):
                with open(os.path.join(root, name), "w") as handle:
                    handle.write("x" * 200)
            with open(os.path.join(root, ".git", "config"), "w") as handle:
                handle.write("x" * 200)

            found = {os.path.relpath(f, root) for f in tools.enumerate_files(root)}
            for banned in (".env", "private.pem", "id_rsa", "credentials.json"):
                self.assertNotIn(banned, found)
            self.assertFalse(any(f.startswith(".git") for f in found))
            self.assertIn("notes.md", found)

    def test_read_text_refuses_a_secret_even_if_a_caller_asks_directly(self):
        """Redundant with enumeration today, which is the point of having it.

        Every current caller reaches read_text through enumerate_files. This guard costs
        one line and means a future one that reads a model-supplied path cannot turn into
        a key disclosure.
        """
        import os
        import tempfile

        from agentaus_bridge import tools

        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, ".env")
            with open(path, "w") as handle:
                handle.write("AGENTAUS_API_KEY=super-secret")
            self.assertEqual(tools.read_text(path), "")


if __name__ == "__main__":
    unittest.main()
