"""Files the bridge must never read, however a search happened to match them.

These reads do not reach Claude Code's permission prompts. That is a deliberate design
choice and it makes this list the only thing standing between a search term that happens
to appear in a key file and that key landing in a transcript.

Found by building a directory of decoys and searching it rather than by reading the code:
matching the whole filename let through `credentials.json`, which is what Google
service-account keys are called, and `id_rsa.bak`, which is a private key with four
characters after it.
"""

import os
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

    def test_bare_credential_files_the_filter_used_to_miss(self):
        """Each is a credential under an unambiguous name that only `.env` used to block.

        Measured: exploration runs read `.git-credentials` and `.npmrc` on 40/40 turns
        with no permission prompt before these were added.
        """
        for name in (".git-credentials", ".npmrc", ".pypirc", ".dockercfg", ".pgpass"):
            self.assertTrue(_is_secret(name), name)


class SecretByWhereItLives(unittest.TestCase):
    """Credentials whose basename is too common to block, but whose directory gives them
    away: `config` under `.kube` is cluster credentials, `config` anywhere else is not.

    The directory rule only applies when the caller supplies the full path - every real
    caller does - so the basename-only form of these names stays readable.
    """

    def test_known_directory_credentials_are_refused_with_a_path(self):
        for path in ("/home/u/.config/gh/hosts.yml", "/home/u/.kube/config",
                     "/home/u/.docker/config.json", "/home/u/.aws/credentials",
                     "/home/u/.aws/config"):
            self.assertTrue(_is_secret(os.path.basename(path), path), path)

    def test_anything_under_ssh_is_refused_at_any_depth(self):
        """Nested keys are still keys: `.ssh/keys/deploy` once passed the parent-only check."""
        for path in ("/home/u/.ssh/config", "/home/u/.ssh/known_hosts",
                     "/home/u/.ssh/my_deploy_key", "/home/u/.ssh/keys/deploy",
                     "/home/u/.ssh/config.d/work"):
            self.assertTrue(_is_secret(os.path.basename(path), path), path)

    def test_a_nested_ssh_key_is_left_out_of_a_walk_and_refused_by_read_text(self):
        import tempfile

        from agentaus_bridge import tools

        with tempfile.TemporaryDirectory() as root:
            keys = os.path.join(root, ".ssh", "keys")
            os.makedirs(keys)
            key = os.path.join(keys, "deploy")
            with open(key, "w") as handle:
                handle.write("PRIVATE KEY " * 20)
            self.assertEqual(tools.enumerate_files(root), [])
            self.assertEqual(tools.read_text(key), "")

    def test_a_symlink_pointing_at_a_key_is_refused(self):
        """`notes.txt -> id_rsa` is the key; the link's own name hid that."""
        import tempfile

        with tempfile.TemporaryDirectory() as root:
            key = os.path.join(root, "id_rsa")
            with open(key, "w") as handle:
                handle.write("PRIVATE KEY")
            link = os.path.join(root, "notes.txt")
            os.symlink(key, link)
            self.assertTrue(_is_secret(os.path.basename(link), link), link)


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

    def test_the_path_aware_rules_do_not_over_block_ordinary_paths(self):
        """An ordinary `config.json` or source file stays readable when a path is passed.

        The directory rules match a specific parent+name; a `config.json` that is not
        under `.docker`, and a `config` that is not under `.kube` or `.ssh`, are code or
        app settings these searches exist to find.
        """
        for path in ("/home/u/project/src/app.py", "/home/u/project/config.json",
                     "/home/u/project/webpack/config", "/home/u/project/gh/notes.md",
                     "/home/u/.ssh-tools/README.md"):
            self.assertFalse(_is_secret(os.path.basename(path), path), path)


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
