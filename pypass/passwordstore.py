#
#    Copyright (C) 2014 Alexandre Viau <alexandre@alexandreviau.net>
#
#    This file is part of python-pass.
#
#    python-pass is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    python-pass is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with python-pass.  If not, see <http://www.gnu.org/licenses/>.
#

import errno
import os
import subprocess
import shutil
import string
import re

from .entry_type import EntryType

# Secure source of randomness for password generation
try:
    from secrets import choice
except ImportError:
    import random
    _system_random = random.SystemRandom()
    choice = _system_random.choice

# Find the right gpg binary
if subprocess.call(
        ['which', 'gpg2'],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE) == 0:
    GPG_BIN = 'gpg2'
elif subprocess.call(
        ['which', 'gpg'],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE) == 0:
    GPG_BIN = 'gpg'
else:
    raise Exception("Could not find GPG")


class PasswordStore(object):
    """This is a Password Store

    :param path: The path of the password-store. By default,
                 '$home/.password-store'.
    :param git_dir: The git directory of the password store. By default,
                    it looks for a .git directory in the password store.
    """

    def __init__(
            self,
            path=os.path.join(os.getenv("HOME"), ".password-store"),
            git_dir=None,
    ):
        self.path = os.path.abspath(path)

        # Check if a main .gpg-id exists
        self._get_gpg_id(self.path)

        # Try to locate the git dir
        git_dir = git_dir or os.path.join(self.path, '.git')
        self.uses_git = os.path.isdir(git_dir)
        if self.uses_git:
            self.git_dir = git_dir

    def _is_valid_store_subpath(self, child_path):
        try:
            # Requires at least Python 3.5
            store_commonpath = os.path.commonpath([self.path])
            child_commonpath = os.path.commonpath([self.path, child_path])
            return store_commonpath == child_commonpath
        except AttributeError:
            # Pre-3.5 fallback
            commonprefix = os.path.commonprefix([self.path, child_path])
            return commonprefix.startswith(self.path)

    def _resolve_path(self, path):
        """Returns the absolute, validated path

        :param path: A relative path in the password store
        """
        file_path = os.path.abspath(os.path.join(self.path, path))
        if self._is_valid_store_subpath(file_path):
            return file_path
        else:
            raise ValueError('Couldn\'t resolve %s' % path)

    def _get_gpg_id(self, file_location):
        file_path = os.path.abspath(file_location)

        while self._is_valid_store_subpath(file_path):
            # Read the .gpg-id
            gpg_id_path = os.path.join(file_path, '.gpg-id')
            if os.path.isfile(gpg_id_path):
                with open(gpg_id_path, 'r') as gpg_id_file:
                    return gpg_id_file.read().strip()

            file_path = os.path.dirname(file_path)

        raise Exception("could not find .gpg-id file")

    def __contains__(self, path):
        """Checks whether `path` is an existing entry

        :param path: The relative path of a possible entry
        :returns: True, if the entry exists
        """
        return os.path.isfile('%s.gpg' % self._resolve_path(path))

    def _walk(self, path='', on_dir=None, on_file=None, topdown=True):
        """Walks the password store from `path` in given direction

        :param path: The relative path of the root of walk. Defaults to ''
        :param on_dir: Called with the absolute path of every visited subdir
        :param on_file: Called with the absolute path of every visited file
        :param topdown: Indicates the direction of walk. Defaults to True
        """
        root = self._resolve_path(path)
        it = os.walk(root, topdown=topdown, followlinks=True)

        for dirpath, dirnames, filenames in it:
            if on_dir is not None:
                for dirname in dirnames:
                    on_dir(os.path.join(dirpath, dirname))
            if on_file is not None:
                for filename in filenames:
                    on_file(os.path.join(dirpath, filename))

    def get_passwords_list(self):
        """Returns a list of the passwords in the store

        :returns: Example: ['Email/bob.net', 'example.com']
        """
        passwords = []
        offset = len(self._resolve_path('')) + 1
        self._walk(
            on_file=lambda path:
                path.endswith('.gpg') and passwords.append(path[offset:-4])
        )
        return passwords

    def get_decrypted_password(self, path, entry=None):
        """Returns the content of the decrypted password file

        :param path: The path of the password to be decrypted. Example:
                     'email.com'
        :param entry: The entry to retreive. (EntryType enum)
        """
        passfile_path = '%s.gpg' % self._resolve_path(path)

        gpg = subprocess.Popen(
            [
                GPG_BIN,
                '--quiet',
                '--batch',
                '--use-agent',
                '-d', passfile_path,
            ],
            shell=False,
            stdout=subprocess.PIPE
        )
        gpg.wait()

        if gpg.returncode == 0:
            decrypted_password = gpg.stdout.read().decode()

            if entry == EntryType.username:
                usr = re.search(
                    '(?:username|user|login): (.+)',
                    decrypted_password
                )
                if usr:
                    return usr.groups()[0]
            elif entry == EntryType.password:
                pw = re.search('(?:password|pass): (.+)', decrypted_password)
                if pw:
                    return pw.groups()[0]
                else:  # If there is no match, password is the first line
                    return decrypted_password.split('\n')[0]
            elif entry == EntryType.hostname:
                hostname = re.search(
                    '(?:host|hostname): (.+)', decrypted_password
                )
                if hostname:
                    return hostname.groups()[0]
            else:
                return decrypted_password
        else:
            raise Exception('Couldn\'t decrypt %s' % path)

    def insert_password(self, path, password):
        """Encrypts the password at the given path

        :param path: Where to insert the password. Ex: 'passwordstore.org'
        :param password: The password to insert, can be multi-line
        """

        passfile_path = '%s.gpg' % self._resolve_path(path)

        if not os.path.isdir(os.path.dirname(passfile_path)):
            os.makedirs(os.path.dirname(passfile_path))

        gpg = subprocess.Popen(
            [
                GPG_BIN,
                '-e',
                '-r', self._get_gpg_id(passfile_path),
                '--batch',
                '--use-agent',
                '--no-tty',
                '--yes',
                '-o', passfile_path
            ],
            shell=False,
            stdin=subprocess.PIPE
        )

        gpg.stdin.write(password.encode())
        gpg.stdin.close()
        gpg.wait()

    def generate_password(
        self,
        path,
        digits=True,
        symbols=True,
        length=25,
        first_line_only=False
    ):
        """Returns and stores a random password

        :param path: Where to insert the password. Ex: 'passwordstore.org'
        :param digits: Should the password have digits? Defaults to True
        :param symbols: Should the password have symbols? Defaults to True
        :param length: Length of the password. Defaults to 25
        :param first_line_only: Modify only the first line of an existing entry
        :returns: Generated password.
        """
        if first_line_only:
            old_content = self.get_decrypted_password(path)
            content_wo_pass = ''.join(old_content.partition('\n')[1:])
        else:
            content_wo_pass = ''

        chars = string.ascii_letters

        if symbols:
            chars += string.punctuation

        if digits:
            chars += string.digits

        password = ''.join(choice(chars) for i in range(length))

        self.insert_password(path, password + content_wo_pass)

        return password

    def remove(self, path, recursive=False, on_entry=lambda _: True):
        """Removes the entry or directory at `path`

        First, the removal of an entry at `path` is attempted.  If this
        succeeds, the function returns.  Second, a directory's removal is
        attempted, but only if `recursive` is true (it's false by default),
        otherwise `ValueError` is thrown.  If nothing was found at `path`,
        an `OSError` is thrown.

        You can provide a callback function `on_entry`, that is called with
        every file's or directory's absolute path, that is a candidate for
        removal.  The callback's return value is interpreted as a `bool`,
        and if it's true, the corresponding file or directory (if empty) is
        removed.  `on_entry` is a constant `True` by default.
        """
        resolved_path = self._resolve_path(path)

        # Remove the path named entry
        if path in self:
            resolved_path += '.gpg'
            if on_entry(resolved_path):
                os.remove(resolved_path)

                # Prune emtpy parent directories
                try:
                    os.removedirs(os.path.dirname(resolved_path))
                except OSError:
                    pass

        # Or remove the directory at path
        elif os.path.isdir(resolved_path):
            if not recursive:
                raise ValueError(
                    '%s is a directory, but recursive is False' % path
                )
            self._walk(
                path,
                topdown=False,
                on_dir=lambda name: on_entry(name) and os.rmdir(name),
                on_file=lambda name: on_entry(name) and os.remove(name)
            )
            try:
                on_entry(resolved_path) and os.rmdir(resolved_path)
            except OSError:
                pass

        else:
            raise OSError(errno.ENOENT, 'Couldn\'t find requested item', path)

    def copy(self, old_path, new_path, on_overwrite=lambda _, __: True):
        """Copies the entry or directory at `old_path` to `new_path`

        First, copying of an entry at `old_path` is attempted.  If this
        succeeds, the function returns.  Second, a directory tree's copying
        is attempted.  If nothing was found at `old_path`, an `OSError` with
        error code `ENOENT` is thrown.

        If `new_path` points to an existing directory, the content of
        `old_path` is copied inside of `new_path`.  Otherwise, `new_path`
        is interpreted as the name of the destination file or root directory.

        You can provide a callback function `on_overwrite`, that is called
        whenever a file would be overwritten.  It receives two arguments:
        the absolute paths of the copied and destination files.  The return
        value is interpreted as a `bool`, and if it's true, the destination
        file is overwritten with the copied one.  `on_overwrite` is a constant
        `True` by default.
        """
        resolved_old_path = self._resolve_path(old_path)
        resolved_new_path = self._resolve_path(new_path)

        if os.path.isdir(resolved_new_path):
            resolved_new_path = os.path.join(
                resolved_new_path,
                os.path.basename(old_path)
            )

        if old_path in self:
            resolved_old_path += '.gpg'
            resolved_new_path += '.gpg'
            if (not os.path.isfile(resolved_new_path) or
                    on_overwrite(resolved_old_path, resolved_new_path)):
                shutil.copy2(resolved_old_path, resolved_new_path)

        elif os.path.isdir(resolved_old_path):
            try:
                os.makedirs(resolved_new_path)
            except OSError:
                pass
            offset = len(resolved_old_path) + 1

            def new(old):
                return os.path.join(resolved_new_path, old[offset:])

            self._walk(
                resolved_old_path,
                on_dir=lambda old:
                    old != resolved_new_path and os.mkdir(new(old)),
                on_file=lambda old:
                    (not os.path.isfile(new(old)) or
                        on_overwrite(old, new(old))) and
                    shutil.copy2(old, new(old))
            )

        else:
            raise OSError(
                errno.ENOENT,
                'Couldn\'t find requested item to copy',
                old_path
            )

    @staticmethod
    def init(gpg_id, path, clone_url=None):
        """Creates a password store to the given path

        :param gpg_id: Default gpg key identification used for encryption and
                       decryption. Example: '3CCC3A3A'
        :param path: Where to create the password store. By default, this is
                     $home/.password-store
        :param clone_url: If specified, the clone_url parameter will be used
                          to import a password store from a git repository.
                          Example: ssh://myserver.net:/home/bob/.password-store
        :returns: PasswordStore object
        """
        git_dir = os.path.join(path, '.git')
        git_work_tree = path

        # Create a folder at the path
        if not os.path.exists(path):
            os.makedirs(path)

        # Clone an existing remote repo
        if clone_url:
            # Init git repo
            subprocess.call(
                [
                    "git",
                    "--git-dir=%s" % git_dir,
                    "--work-tree=%s" % git_work_tree,
                    "init", path
                ],
                shell=False
            )

            # Add remote repo
            subprocess.call(
                [
                    "git",
                    "--git-dir=%s" % git_dir,
                    "--work-tree=%s" % git_work_tree,
                    "remote",
                    "add",
                    "origin",
                    clone_url
                ],
                shell=False,
            )

            # Pull remote repo
            # TODO: add parameters for remote and branch ?
            subprocess.call(
                [
                    "git",
                    "--git-dir=%s" % git_dir,
                    "--work-tree=%s" % git_work_tree,
                    "pull",
                    "origin",
                    "master"
                ],
                shell=False
            )

        gpg_id_path = os.path.join(path, '.gpg-id')
        if os.path.exists(gpg_id_path) is False:
            # Create .gpg_id and put the gpg id in it
            with open(gpg_id_path, 'a') as gpg_id_file:
                gpg_id_file.write(gpg_id + '\n')

        return PasswordStore(path)

    def git_init(self, git_dir=None):
        """Transform  the existing password store into a git repository

        :param git_dir: Where to create the git directory. By default, it will
                        be created at the root of the password store in a .git
                        folder.
        """

        self.git_dir = git_dir or os.path.join(self.path, '.git')
        self.uses_git = True

        subprocess.call(
            [
                'git',
                "--git-dir=%s" % self.git_dir,
                "--work-tree=%s" % self.path,
                'init',
            ],
            shell=False
        )

        self.git_add_and_commit(
            '.',
            message="Add current contents of password store."
        )

        # Create .gitattributes and commit it
        with open(
                os.path.join(self.path, '.gitattributes'), 'w'
        ) as gitattributes:
            gitattributes.write('*.gpg diff=gpg\n')

        self.git_add_and_commit(
            '.gitattributes',
            message="Configure git repository for gpg file diff."
        )

        subprocess.call(
            [
                'git',
                "--git-dir=%s" % self.git_dir,
                "--work-tree=%s" % self.path,
                'config',
                '--local',
                'diff.gpg.binary',
                'true'
            ],
            shell=False
        )

        subprocess.call(
            [
                'git',
                "--git-dir=%s" % self.git_dir,
                "--work-tree=%s" % self.path,
                'config',
                '--local',
                'diff.gpg.textconv',
                'gpg -d'
            ],
            shell=False
        )

    def git_add_and_commit(self, path, message=None):

        subprocess.call(
            [
                'git',
                "--git-dir=%s" % self.git_dir,
                "--work-tree=%s" % self.path,
                'add',
                path
            ],
            shell=False
        )

        if message:
            subprocess.call(
                [
                    'git',
                    "--git-dir=%s" % self.git_dir,
                    "--work-tree=%s" % self.path,
                    'commit',
                    '-m',
                    message
                ],
                shell=False
            )
        else:
            subprocess.call(
                [
                    'git',
                    "--git-dir=%s" % self.git_dir,
                    "--work-tree=%s" % self.path,
                    'commit'
                ],
                shell=False
            )
