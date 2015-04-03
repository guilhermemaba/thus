#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
#  installation_process.py
#
#  This file was forked from Cnchi (graphical installer from Antergos)
#  Check it at https://github.com/antergos
#
#  Copyright 2013 Antergos (http://antergos.com/)
#  Copyright 2013 Manjaro (http://manjaro.org)
#
#  This program is free software; you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation; either version 2 of the License, or
#  (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program; if not, write to the Free Software
#  Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston,
#  MA 02110-1301, USA.

""" Installation thread module. Where the real installation happens """

import crypt
import logging
import multiprocessing
import os
import collections
import queue
import shutil
import subprocess
import sys
import time
import urllib.request
import urllib.error
 
import traceback

import parted3.fs_module as fs
import misc.misc as misc
import info
import encfs
from installation import auto_partition
from installation import chroot
from installation import mkinitcpio

from configobj import ConfigObj

conf_file = '/etc/thus.conf'
configuration = ConfigObj(conf_file)
MHWD_SCRIPT = 'mhwd.sh'
DEST_DIR = "/install"

DesktopEnvironment = collections.namedtuple('DesktopEnvironment', ['executable', 'desktop_file'])

desktop_environments = [
    DesktopEnvironment('/usr/bin/startkde', 'plasma'), # KDE Plasma 5
    DesktopEnvironment('/usr/bin/startkde', 'kde-plasma'), # KDE Plasma 4
    DesktopEnvironment('/usr/bin/gnome-session', 'gnome'),
    DesktopEnvironment('/usr/bin/startxfce4', 'xfce'),
    DesktopEnvironment('/usr/bin/cinnamon-session', 'cinnamon-session'),
    DesktopEnvironment('/usr/bin/mate-session', 'mate'),
    DesktopEnvironment('/usr/bin/enlightenment_start', 'enlightenment'),
    DesktopEnvironment('/usr/bin/lxsession', 'LXDE'),
    DesktopEnvironment('/usr/bin/startlxde', 'LXDE'),
    DesktopEnvironment('/usr/bin/lxqt-session', 'lxqt'),
    DesktopEnvironment('/usr/bin/pekwm', 'pekwm'),
    DesktopEnvironment('/usr/bin/openbox-session', 'openbox')
]


def chroot_run(cmd):
    chroot.run(cmd, DEST_DIR)


def write_file(filecontents, filename):
    """ writes a string of data to disk """
    if not os.path.exists(os.path.dirname(filename)):
        os.makedirs(os.path.dirname(filename))

    with open(filename, "w") as fh:
        fh.write(filecontents)

## BEGIN: RSYNC-based file copy support
#CMD = 'unsquashfs -f -i -da 32 -fr 32 -d %(dest)s %(source)s'
CMD = 'rsync -ar --progress %(source)s %(dest)s'
PERCENTAGE_FORMAT = '%d/%d ( %.2f %% )'
from threading import Thread
import re
ON_POSIX = 'posix' in sys.builtin_module_names

class FileCopyThread(Thread):
    """ Update the value of the progress bar so that we get some movement """
    def __init__(self, installer, current_file, total_files, source, dest, offset=0):
        # Environment used for executing rsync properly
        # Setting locale to C (fix issue with tr_TR locale)
        self.at_env=os.environ
        self.at_env["LC_ALL"]="C"

        self.our_current = current_file
        self.process = subprocess.Popen(
            (CMD % {
                'source': source,
                'dest': dest,
            }).split(),
            env=self.at_env,
            bufsize=1,
            stdout=subprocess.PIPE,
            close_fds=ON_POSIX
        )
        self.installer = installer
        self.total_files = total_files
        # in order for the progressbar to pick up where the last rsync ended,
        # we need to set the offset (because the total number of files is calculated before)
        self.offset = offset
        super(FileCopyThread, self).__init__()

    def kill(self):
        if self.process.poll() is None:
            self.process.kill()

    def update_label(self, text):
        self.installer.queue_event('info', _("Copying '/%s'") % text)

    def update_progress(self, num_files):
        progress = (float(num_files) / float(self.total_files))
        self.installer.queue_event('percent', progress)
        #self.installer.queue_event('progress-info', PERCENTAGE_FORMAT % (num_files, self.total_files, (progress*100)))

    def run(self):
        num_files_copied = 0
        for line in iter(self.process.stdout.readline, b''):
            # small comment on this regexp.
            # rsync outputs three parameters in the progress.
            # xfer#x => i try to interpret it as 'file copy try no. x'
            # to-check=x/y, where:
            #  - x = number of files yet to be checked
            #  - y = currently calculated total number of files.
            # but if you're copying directory with some links in it, the xfer# might not be a
            # reliable counter. ( for one increase of xfer, many files may be created)
            # In case of manjaro, we pre-compute the total number of files.
            # therefore we can easily subtract x from y in order to get real files copied / processed count.
            m = re.findall(r'xfr#(\d+), ir-chk=(\d+)/(\d+)', line.decode())
            if m:
                # we've got a percentage update
                num_files_remaining = int(m[0][1])
                num_files_total_local = int(m[0][2])
                # adjusting the offset so that progressbar can be continuesly drawn
                num_files_copied = num_files_total_local - num_files_remaining + self.offset
                if num_files_copied % 100 == 0:
                    self.update_progress(num_files_copied)
            # Disabled until we find a proper solution for BadDrawable (invalid Pixmap or Window parameter) errors
            # Details: serial YYYYY error_code 9 request_code 62 minor_code 0
            # This might even speed up the copy process ...
            """else:
                # we've got a filename!
                if num_files_copied % 100 == 0:
                    self.update_label(line.decode().strip())"""

        self.offset = num_files_copied

## END: RSYNC-based file copy support


class InstallError(Exception):
    """ Exception class called upon an installer error """
    def __init__(self, value):
        """ Initialize exception class """
        super().__init__(value)
        self.value = value

    def __str__(self):
        """ Returns exception message """
        return repr(self.value)


class InstallationProcess(multiprocessing.Process):
    """ Installation process thread class """
    def __init__(self, settings, callback_queue, mount_devices,
                 fs_devices, alternate_package_list="", ssd=None, blvm=False):
        """ Initialize installation class """
        multiprocessing.Process.__init__(self)

        self.alternate_package_list = alternate_package_list

        self.callback_queue = callback_queue
        self.settings = settings
        self.method = self.settings.get('partition_mode')
        msg = _("Installing using the '{0}' method").format(self.method)
        self.queue_event('info', msg)

        # This flag tells us if there is a lvm partition (from advanced install)
        # If it's true we'll have to add the 'lvm2' hook to mkinitcpio
        self.blvm = blvm

        if ssd is not None:
            self.ssd = ssd
        else:
            self.ssd = {}

        self.mount_devices = mount_devices

        # Set defaults
        self.desktop_manager = 'none'
        self.network_manager = 'NetworkManager'
        self.card = []
        # Packages to be removed
        self.conflicts = []

        self.fs_devices = fs_devices

        self.running = True
        self.error = False

        self.special_dirs_mounted = False

        # Initialize some vars that are correctly initialized elsewhere (pylint complains about it)
        self.auto_device = ""
        self.arch = ""
        self.initramfs = ""
        self.kernel = ""
        self.vmlinuz = ""
        self.dest_dir = ""
        self.bootloader_ok = self.settings.get('bootloader_ok')

    def queue_fatal_event(self, txt):
        """ Queues the fatal event and exits process """
        self.error = True
        self.running = False
        self.queue_event('error', txt)
        self.callback_queue.join()
        # Is this really necessary?
        os._exit(0)

    def queue_event(self, event_type, event_text=""):
        if self.callback_queue is not None:
            try:
                self.callback_queue.put_nowait((event_type, event_text))
            except queue.Full:
                pass
        else:
            print("{0}: {1}".format(event_type, event_text))

    def wait_for_empty_queue(self, timeout):
        if self.callback_queue is not None:
            tries = 0
            if timeout < 1:
                timeout = 1
            while tries < timeout and not self.callback_queue.empty():
                time.sleep(1)
                tries += 1

    def run(self):
        """ Calls run_installation and takes care of exceptions """

        try:
            self.run_installation()
        except subprocess.CalledProcessError as process_error:
            exc_type, exc_value, exc_traceback = sys.exc_info()
            trace = repr(traceback.format_exception(exc_type, exc_value, exc_traceback))
            logging.error(_("Error running command %s"), process_error.cmd)
            logging.error(_("Output: %s"), process_error.output)
            logging.error(trace)
            self.queue_fatal_event(process_error.output)
        except (
                InstallError, pyalpm.error, KeyboardInterrupt, TypeError, AttributeError, OSError,
                IOError) as install_error:
            exc_type, exc_value, exc_traceback = sys.exc_info()
            trace = repr(traceback.format_exception(exc_type, exc_value, exc_traceback))
            logging.error(install_error)
            logging.error(trace)
            self.queue_fatal_event(install_error)

    @misc.raise_privileges
    def run_installation(self):
        """ Run installation """

        '''
        From this point, on a warning situation, Cnchi should try to continue, so we need to catch the exception here.
        If we don't catch the exception here, it will be catched in run() and managed as a fatal error.
        On the other hand, if we want to clarify the exception message we can catch it here
        and then raise an InstallError exception.
        '''

        # Common vars
        self.packages = []

        if not os.path.exists(DEST_DIR):
            with misc.raised_privileges():
                os.makedirs(DEST_DIR)
        else:
            # If we're recovering from a failed/stoped install, there'll be
            # some mounted directories. Try to unmount them first.
            # We use unmount_all from auto_partition to do this.
            auto_partition.unmount_all(DEST_DIR)

        # get settings
        self.distribution_name = configuration['distribution']['DISTRIBUTION_NAME']
        self.distribution_version = configuration['distribution']['DISTRIBUTION_VERSION']
        self.live_user = configuration['install']['LIVE_USER_NAME']
        self.media = configuration['install']['LIVE_MEDIA_SOURCE']
        self.media_desktop = configuration['install']['LIVE_MEDIA_DESKTOP']
        self.media_type = configuration['install']['LIVE_MEDIA_TYPE']
        self.kernel = configuration['install']['KERNEL']

        self.vmlinuz = "vmlinuz-%s" % self.kernel
        self.initramfs = "initramfs-%s" % self.kernel

        self.arch = os.uname()[-1]

        # Create and format partitions

        if self.method == 'automatic':
            self.auto_device = self.settings.get('auto_device')

            logging.debug(_("Creating partitions and their filesystems in %s"), self.auto_device)

            # If no key password is given a key file is generated and stored in /boot
            # (see auto_partition.py)

            auto = auto_partition.AutoPartition(dest_dir=DEST_DIR,
                                                auto_device=self.auto_device,
                                                use_luks=self.settings.get("use_luks"),
                                                luks_password=self.settings.get("luks_root_password"),
                                                use_lvm=self.settings.get("use_lvm"),
                                                use_home=self.settings.get("use_home"),
                                                callback_queue=self.callback_queue)
            auto.run()

            # Get mount_devices and fs_devices
            # (mount_devices will be used when configuring GRUB in modify_grub_default)
            # (fs_devices  will be used when configuring the fstab file)
            self.mount_devices = auto.get_mount_devices()
            self.fs_devices = auto.get_fs_devices()

        # Create the directory where we will mount our new root partition
        if not os.path.exists(DEST_DIR):
            os.mkdir(DEST_DIR)

        if self.method == 'alongside' or self.method == 'advanced':
            root_partition = self.mount_devices["/"]

            # NOTE: Advanced method formats root by default in installation_advanced

            # if root_partition in self.fs_devices:
            #     root_fs = self.fs_devices[root_partition]
            # else:
            #    root_fs = "ext4"

            if "/boot" in self.mount_devices:
                boot_partition = self.mount_devices["/boot"]
            else:
                boot_partition = ""

            if "swap" in self.mount_devices:
                swap_partition = self.mount_devices["swap"]
            else:
                swap_partition = ""

            # Mount root and boot partitions (only if it's needed)
            # Not doing this in automatic mode as AutoPartition class mounts the root and boot devices itself.
            txt = _("Mounting partition {0} into {1} directory").format(root_partition, DEST_DIR)
            logging.debug(txt)
            subprocess.check_call(['mount', root_partition, DEST_DIR])
            # We also mount the boot partition if it's needed
            boot_path = os.path.join(DEST_DIR, "boot")
            if not os.path.exists(boot_path):
                os.makedirs(boot_path)
            if "/boot" in self.mount_devices:
                txt = _("Mounting partition {0} into {1}/boot directory")
                txt = txt.format(boot_partition, boot_path)
                logging.debug(txt)
                subprocess.check_call(['mount', boot_partition, boot_path])

            # In advanced mode, mount all partitions (root and boot are already mounted)
            if self.method == 'advanced':
                for path in self.mount_devices:
                    # Ignore devices without a mount path (or they will be mounted at "DEST_DIR")
                    if path == "":
                        continue
                    mount_part = self.mount_devices[path]
                    if mount_part != root_partition and mount_part != boot_partition and mount_part != swap_partition:
                        mount_dir = os.path.join(DEST_DIR, path)
                        try:
                            if not os.path.exists(mount_dir):
                                os.makedirs(mount_dir)
                            txt = _("Mounting partition {0} into {1} directory")
                            txt = txt.format(mount_part, mount_dir)
                            logging.debug(txt)
                            subprocess.check_call(['mount', mount_part, mount_dir])
                        except subprocess.CalledProcessError as process_error:
                            # We will continue as root and boot are already mounted
                            logging.warning(_("Can't mount %s in %s"), mount_part, mount_dir)
                            logging.warning(_("Command %s has failed."), process_error.cmd)
                            logging.warning(_("Output : %s"), process_error.output)
                    elif mount_part == swap_partition:
                        try:
                            logging.debug(_("Activating swap in %s"), mount_part)
                            subprocess.check_call(['swapon', swap_partition])
                        except subprocess.CalledProcessError as process_error:
                            # We can continue even if no swap is on
                            logging.warning(_("Can't activate swap in %s"), mount_part)
                            logging.warning(_("Command %s has failed."), process_error.cmd)
                            logging.warning(_("Output : %s"), process_error.output)

        # Nasty workaround:
        # If pacman was stoped and /var is in another partition than root
        # (so as to be able to resume install), database lock file will still be in place.
        # We must delete it or this new installation will fail
        db_lock = os.path.join(DEST_DIR, "var/lib/pacman/db.lck")
        if os.path.exists(db_lock):
            with misc.raised_privileges():
                os.remove(db_lock)
            logging.debug(_("%s deleted"), db_lock)

        # Create some needed folders
        folders = [
            os.path.join(DEST_DIR, 'var/lib/pacman'),
            os.path.join(DEST_DIR, 'etc/pacman.d/gnupg'),
            os.path.join(DEST_DIR, 'var/log')]

        for folder in folders:
            if not os.path.exists(folder):
                os.makedirs(folder)

        all_ok = True

        try:
            self.queue_event('debug', _('Install System ...'))
            # very slow ...
            self.install_system()

            subprocess.check_call(['mkdir', '-p', '%s/var/log/' % self.dest_dir])
            self.queue_event('debug', _('System installed.'))

            self.queue_event('debug', _('Configuring system ...'))
            self.configure_system()
            self.queue_event('debug', _('System configured.'))

            # Install boot loader (always after running mkinitcpio)
            if self.settings.get('install_bootloader'):
                self.queue_event('debug', _('Installing boot loader ...'))
                self.install_bootloader()

        except subprocess.CalledProcessError as err:
            logging.error(err)
            self.queue_fatal_event("CalledProcessError.output = %s" % err.output)
            all_ok = False
        except InstallError as err:
            logging.error(err)
            self.queue_fatal_event(err.value)
            all_ok = False
        except Exception as err:
            try:
                logging.debug('Exception: %s. Trying to continue.' % err)
                all_ok = True
                pass
            except Exception as err:
                txt = ('Unknown Error: %s. Unable to continue.' % err)
                logging.debug(txt)
                self.queue_fatal_event(txt)
                self.running = False
                self.error = True
                all_ok = False

        if all_ok is False:
            self.error = True
            return False
        else:
            # Last but not least, copy Thus log to new installation
            datetime = time.strftime("%Y%m%d") + "-" + time.strftime("%H%M%S")
            dst = os.path.join(self.dest_dir, "var/log/thus-%s.log" % datetime)
            try:
                shutil.copy("/tmp/thus.log", dst)
            except FileNotFoundError:
                logging.warning(_("Can't copy Thus log to %s") % dst)
            except FileExistsError:
                pass
            # Unmount everything
            chroot_run_umount_special_dirs()
            source_dirs = {"source", "source_desktop"}
            for p in source_dirs:
                p = os.path.join("/", p)
                (fsname, fstype, writable) = misc.mount_info(p)
                if fsname:
                    try:
                        txt = _("Unmounting %s") % p
                        self.queue_event('debug', txt)
                        subprocess.check_call(['umount', p])
                    except subprocess.CalledProcessError as err:
                        logging.error(err)
                        try:
                            subprocess.check_call(["umount", "-l", p])
                        except subprocess.CalledProcessError as err:
                            self.queue_event('warning', _("Can't unmount %s") % p)
                            logging.warning(err)
            self.queue_event('debug', "Mounted devices: %s" % self.mount_devices)
            for path in self.mount_devices:
                mount_part = self.mount_devices[path]
                mount_dir = self.dest_dir + path
                if path != '/' and path != 'swap' and path != '':
                    try:

                        txt = _("Unmounting %s") % mount_dir
                        self.queue_event('debug', txt)
                        subprocess.check_call(['umount', mount_dir])
                    except subprocess.CalledProcessError as err:
                        logging.error(err)
                        try:
                            subprocess.check_call(["umount", "-l", mount_dir])
                        except subprocess.CalledProcessError as err:
                            # We will continue as root and boot are already mounted
                            logging.warning(err)
                            self.queue_event('debug', _("Can't unmount %s") % mount_dir)
            # now we can unmount /install
            (fsname, fstype, writable) = misc.mount_info(self.dest_dir)
            if fsname:
                try:
                    txt = _("Unmounting %s") % self.dest_dir
                    self.queue_event('debug', txt)
                    subprocess.check_call(['umount', self.dest_dir])
                except subprocess.CalledProcessError as err:
                    logging.error(err)
                    try:
                        subprocess.check_call(["umount", "-l", self.dest_dir])
                    except subprocess.CalledProcessError as err:
                        logging.warning(err)
                        self.queue_event('debug', _("Can't unmount %s") % p)
            # Installation finished successfully
            self.queue_event("finished", _("Installation finished successfully."))
            self.running = False
            self.error = False
            return True

    def check_source_folder(self, mount_point):
        """ Check if source folders are mounted """
        device = None
        with open('/proc/mounts', 'r') as fp:
            for line in fp:
                line = line.split()
                if line[1] == mount_point:
                    device = line[0]
        return device

    def install_system(self):
        """ Copies all files to target """
        # mount the media location.
        try:
            if(not os.path.exists(self.dest_dir)):
                os.mkdir(self.dest_dir)
            if(not os.path.exists("/source")):
                os.mkdir("/source")
            if(not os.path.exists("/source_desktop")):
                os.mkdir("/source_desktop")
            # find the squashfs..
            if(not os.path.exists(self.media)):
                txt = _("Base filesystem does not exist! Critical error (exiting).")
                logging.error(txt)
                self.queue_fatal_event(txt)
            if(not os.path.exists(self.media_desktop)):
                txt = _("Desktop filesystem does not exist! Critical error (exiting).")
                logging.error(txt)
                self.queue_fatal_event(txt)

            # Mount the installation media
            mount_point = "/source"
            device = self.check_source_folder(mount_point)
            if device is None:
                subprocess.check_call(["mount", self.media, mount_point, "-t", self.media_type, "-o", "loop"])
            else:
                logging.warning(_("%s is already mounted at %s as %s") % (self.media, mount_point, device))
            mount_point = "/source_desktop"
            device = self.check_source_folder(mount_point)
            if device is None:
                subprocess.check_call(["mount", self.media_desktop, mount_point, "-t", self.media_type, "-o", "loop"])
            else:
                logging.warning(_("%s is already mounted at %s as %s") % (self.media_desktop, mount_point, device))

            # walk root filesystem
            SOURCE = "/source/"
            DEST = self.dest_dir
            directory_times = []
            # index the files
            self.queue_event('info', "Indexing files to be copied...")
            p1 = subprocess.Popen(["unsquashfs", "-l", self.media], stdout=subprocess.PIPE)
            p2 = subprocess.Popen(["wc", "-l"], stdin=p1.stdout, stdout=subprocess.PIPE)
            output1 = p2.communicate()[0]
            self.queue_event('info', _("Indexing files to be copied ..."))
            p1 = subprocess.Popen(["unsquashfs", "-l", self.media_desktop], stdout=subprocess.PIPE)
            p2 = subprocess.Popen(["wc", "-l"], stdin=p1.stdout, stdout=subprocess.PIPE)
            output2 = p2.communicate()[0]
            our_total = int(float(output1) + float(output2))
            self.queue_event('info', _("Extracting root-image ..."))
            our_current = 0
            #t = FileCopyThread(self, our_total, self.media, DEST)
            t = FileCopyThread(self, our_current, our_total, SOURCE, DEST)
            t.start()
            t.join()
            # walk desktop filesystem
            SOURCE = "/source_desktop/"
            DEST = self.dest_dir
            directory_times = []
            self.queue_event('info', _("Extracting desktop-image ..."))
            our_current = int(output1)
            #t = FileCopyThread(self, our_total, self.media_desktop, DEST)
            t = FileCopyThread(self, our_current, our_total, SOURCE, DEST, t.offset)
            t.start()
            t.join()
            # this is purely out of aesthetic reasons. Because we're reading of the queue
            # once 3 seconds, good chances are we're going to miss the 100% file copy.
            # therefore it would be nice to show 100% to the user so he doesn't panick that
            # not all of the files copied.
            self.queue_event('percent', 1.00)
            self.queue_event('progress-info', PERCENTAGE_FORMAT % (our_total, our_total, 100))
            for dirtime in directory_times:
                (directory, atime, mtime) = dirtime
                try:
                    self.queue_event('info', _("Restoring meta-information on %s") % directory)
                    os.utime(directory, (atime, mtime))
                except OSError:
                    pass

        except Exception as err:
            logging.error(err)
            self.queue_fatal_event(err)
            import traceback
            exc_type, exc_value, exc_traceback = sys.exc_info()
            traceback.print_tb(exc_traceback, limit=1, file=sys.stdout)

    def chroot_mount_special_dirs(self):
        """ Mount special directories for our chroot """
        # Don't try to remount them
        if self.special_dirs_mounted:
            self.queue_event('debug', _("Special dirs already mounted."))
            return

        special_dirs = ["sys", "proc", "dev", "dev/pts", "sys/firmware/efi"]
        for s_dir in special_dirs:
            mydir = os.path.join(self.dest_dir, s_dir)
            if not os.path.exists(mydir):
                os.makedirs(mydir)

        mydir = os.path.join(self.dest_dir, "sys")
        subprocess.check_call(["mount", "-t", "sysfs", "/sys", mydir])
        subprocess.check_call(["chmod", "555", mydir])

        mydir = os.path.join(self.dest_dir, "proc")
        subprocess.check_call(["mount", "-t", "proc", "/proc", mydir])
        subprocess.check_call(["chmod", "555", mydir])

        mydir = os.path.join(self.dest_dir, "dev")
        subprocess.check_call(["mount", "-o", "bind", "/dev", mydir])

        mydir = os.path.join(self.dest_dir, "dev/pts")
        subprocess.check_call(["mount", "-t", "devpts", "/dev/pts", mydir])
        subprocess.check_call(["chmod", "555", mydir])

        if self.settings.get('efi'):
            mydir = os.path.join(self.dest_dir, "sys/firmware/efi")
            subprocess.check_call(["mount", "-o", "bind", "/sys/firmware/efi", mydir])

        self.special_dirs_mounted = True

    def chroot_umount_special_dirs(self):
        """ Umount special directories for our chroot """
        # Do not umount if they're not mounted
        if not self.special_dirs_mounted:
            self.queue_event('debug', _("Special dirs are not mounted. Skipping."))
            return

        if self.settings.get('efi'):
            special_dirs = ["dev/pts", "sys/firmware/efi", "sys", "proc", "dev"]
        else:
            special_dirs = ["dev/pts", "sys", "proc", "dev"]

        for s_dir in special_dirs:
            mydir = os.path.join(self.dest_dir, s_dir)
            try:
                subprocess.check_call(["umount", mydir])
            except subprocess.CalledProcessError as err:
                logging.error(err)
                try:
                    subprocess.check_call(["umount", "-l", mydir])
                except subprocess.CalledProcessError as err:
                    self.queue_event('warning', _("Unable to umount %s") % mydir)
                    cmd = _("Command %s has failed.") % err.cmd
                    logging.warning(cmd)
                    out = _("Output : %s") % err.output
                    logging.warning(out)
            except Exception as err:
                self.queue_event('warning', _("Unable to umount %s") % mydir)
                logging.error(err)

        self.special_dirs_mounted = False

    def chroot(self, cmd, timeout=None, stdin=None):
        """ Runs command inside the chroot """
        run = ['chroot', self.dest_dir]

        for element in cmd:
            run.append(element)

        try:
            proc = subprocess.Popen(run,
                                    stdin=stdin,
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT)
            out = proc.communicate(timeout=timeout)[0]
            txt = out.decode()
            if len(txt) > 0:
                logging.debug(txt)
        except OSError as err:
            logging.exception(_("Error running command: %s"), err.strerror)
            raise
        except subprocess.TimeoutExpired as err:
            logging.exception(_("Timeout running command: %s"), run)
            raise

    def is_running(self):
        """ Checks if thread is running """
        return self.running

    def is_ok(self):
        """ Checks if an error has been issued """
        return not self.error

    @staticmethod
    def copy_network_config():
        """ Copies Network Manager configuration """
        source_nm = "/etc/NetworkManager/system-connections/"
        target_nm = os.path.join(DEST_DIR, "etc/NetworkManager/system-connections")

        # Sanity checks.  We don't want to do anything if a network
        # configuration already exists on the target
        if os.path.exists(source_nm) and os.path.exists(target_nm):
            for network in os.listdir(source_nm):
                # Skip LTSP live
                if network == "LTSP":
                    continue

                source_network = os.path.join(source_nm, network)
                target_network = os.path.join(target_nm, network)

                if os.path.exists(target_network):
                    continue

                try:
                    shutil.copy(source_network, target_network)
                except FileNotFoundError:
                    logging.warning(_("Can't copy network configuration files"))
                except FileExistsError:
                    pass

    def auto_fstab(self):
        """ Create /etc/fstab file """

        all_lines = ["# /etc/fstab: static file system information.",
                     "#",
                     "# Use 'blkid' to print the universally unique identifier for a",
                     "# device; this may be used with UUID= as a more robust way to name devices",
                     "# that works even if disks are added and removed. See fstab(5).",
                     "#",
                     "# <file system> <mount point>   <type>  <options>       <dump>  <pass>",
                     "#"]

        use_luks = self.settings.get("use_luks")
        use_lvm = self.settings.get("use_lvm")

        for mount_point in self.mount_devices:
            partition_path = self.mount_devices[mount_point]
            part_info = fs.get_info(partition_path)
            uuid = part_info['UUID']

            if partition_path in self.fs_devices:
                myfmt = self.fs_devices[partition_path]
            else:
                # It hasn't any filesystem defined, skip it.
                continue

            # Take care of swap partitions
            if "swap" in myfmt:
                # If using a TRIM supported SSD, discard is a valid mount option for swap
                if partition_path in self.ssd:
                    opts = "defaults,discard"
                else:
                    opts = "defaults"
                txt = "UUID={0} swap swap {1} 0 0".format(uuid, opts)
                all_lines.append(txt)
                logging.debug(_("Added to fstab : %s"), txt)
                continue

            crypttab_path = os.path.join(DEST_DIR, 'etc/crypttab')

            # Fix for home + luks, no lvm (from Automatic Install)
            if "/home" in mount_point and self.method == "automatic" and use_luks and not use_lvm:
                # Modify the crypttab file
                luks_root_password = self.settings.get("luks_root_password")
                if luks_root_password and len(luks_root_password) > 0:
                    # Use password and not a keyfile
                    home_keyfile = "none"
                else:
                    # Use a keyfile
                    home_keyfile = "/etc/luks-keys/home"

                os.chmod(crypttab_path, 0o666)
                with open(crypttab_path, 'a') as crypttab_file:
                    line = "cryptAntergosHome /dev/disk/by-uuid/{0} {1} luks\n".format(uuid, home_keyfile)
                    crypttab_file.write(line)
                    logging.debug(_("Added to crypttab : %s"), line)
                os.chmod(crypttab_path, 0o600)

                # Add line to fstab
                txt = "/dev/mapper/cryptAntergosHome {0} {1} defaults 0 0".format(mount_point, myfmt)
                all_lines.append(txt)
                logging.debug(_("Added to fstab : %s"), txt)
                continue

            # Add all LUKS partitions from Advanced Install (except root).
            if self.method == "advanced" and mount_point is not "/" and use_luks and "/dev/mapper" in partition_path:
                os.chmod(crypttab_path, 0o666)
                vol_name = partition_path[len("/dev/mapper/"):]
                with open(crypttab_path, 'a') as crypttab_file:
                    line = "{0} /dev/disk/by-uuid/{1} none luks\n".format(vol_name, uuid)
                    crypttab_file.write(line)
                    logging.debug(_("Added to crypttab : %s"), line)
                os.chmod(crypttab_path, 0o600)

                txt = "{0} {1} {2} defaults 0 0".format(partition_path, mount_point, myfmt)
                all_lines.append(txt)
                logging.debug(_("Added to fstab : %s"), txt)
                continue

            # fstab uses vfat to mount fat16 and fat32 partitions
            if "fat" in myfmt:
                myfmt = 'vfat'

            if "btrfs" in myfmt:
                self.settings.set('btrfs', True)

            # Avoid adding a partition to fstab when it has no mount point (swap has been checked above)
            if mount_point == "":
                continue

            # Create mount point on destination system if it yet doesn't exist
            full_path = os.path.join(DEST_DIR, mount_point)
            if not os.path.exists(full_path):
                os.makedirs(full_path)

            # Is ssd ?
            is_ssd = False
            for ssd_device in self.ssd:
                if ssd_device in partition_path:
                    is_ssd = True

            # Add mount options parameters
            if not is_ssd:
                if "btrfs" in myfmt:
                    opts = 'defaults,rw,relatime,space_cache,autodefrag,inode_cache'
                elif "f2fs" in myfmt:
                    opts = 'defaults,rw,noatime'
                elif "ext3" in myfmt or "ext4" in myfmt:
                    opts = 'defaults,rw,relatime,data=ordered'
                else:
                    opts = "defaults,rw,relatime"
            else:
                # As of linux kernel version 3.7, the following
                # filesystems support TRIM: ext4, btrfs, JFS, and XFS.
                if myfmt == 'ext4' or myfmt == 'jfs' or myfmt == 'xfs':
                    opts = 'defaults,rw,noatime,discard'
                elif myfmt == 'btrfs':
                    opts = 'defaults,rw,noatime,compress=lzo,ssd,discard,space_cache,autodefrag,inode_cache'
                else:
                    opts = 'defaults,rw,noatime'

            no_check = ["btrfs", "f2fs"]

            if mount_point == "/" and myfmt not in no_check:
                chk = '1'
            else:
                chk = '0'

            if mount_point == "/":
                self.settings.set('ruuid', uuid)

            txt = "UUID={0} {1} {2} {3} 0 {4}".format(uuid, mount_point, myfmt, opts, chk)
            all_lines.append(txt)
            logging.debug(_("Added to fstab : %s"), txt)

        # Create tmpfs line in fstab
        tmpfs = "tmpfs /tmp tmpfs defaults,noatime,mode=1777 0 0"
        all_lines.append(tmpfs)
        logging.debug(_("Added to fstab : %s"), tmpfs)

        full_text = '\n'.join(all_lines) + '\n'

        fstab_path = os.path.join(DEST_DIR, 'etc/fstab')
        with open(fstab_path, 'w') as fstab_file:
            fstab_file.write(full_text)

        logging.debug(_("fstab written."))

    @staticmethod
    def enable_services(services):
        """ Enables all services that are in the list 'services' """
        for name in services:
            path = os.path.join(DEST_DIR, "usr/lib/systemd/system/{0}.service".format(name))
            if os.path.exists(path):
                chroot_run(['systemctl', '-f', 'enable', name])
                logging.debug(_("Enabled %s service."), name)
            else:
                logging.warning(_("Can't find service %s"), name)

    @staticmethod
    def change_user_password(user, new_password):
        """ Changes the user's password """
        try:
            shadow_password = crypt.crypt(new_password, "$6${0}$".format(user))
        except:
            logging.warning(_("Error creating password hash for user %s"), user)
            return False

        try:
            chroot_run(['usermod', '-p', shadow_password, user])
        except:
            logging.warning(_("Error changing password for user %s"), user)
            return False

        return True

    @staticmethod
    def auto_timesetting():
        """ Set hardware clock """
        subprocess.check_call(["hwclock", "--systohc", "--utc"])
        shutil.copy2("/etc/adjtime", os.path.join(DEST_DIR, "etc/"))

    @staticmethod
    def uncomment_locale_gen(locale):
        """ Uncomment selected locale in /etc/locale.gen """

        path = os.path.join(DEST_DIR, "etc/locale.gen")

        if os.path.exists(path):
            with open(path) as gen:
                text = gen.readlines()

            with open(path, "w") as gen:
                for line in text:
                    if locale in line and line[0] == "#":
                        # remove trailing '#'
                        line = line[1:]
                    gen.write(line)
        else:
            logging.warning(_("Can't find locale.gen file"))

    @staticmethod
    def check_output(command):
        """ Helper function to run a command """
        return subprocess.check_output(command.split()).decode().strip("\n")

    def find_desktop_environment(self):
        for desktop_environment in desktop_environments:
            if os.path.exists('%s%s' % (self.dest_dir, desktop_environment.executable)) \
               and os.path.exists('%s/usr/share/xsessions/%s.desktop' % (self.dest_dir, desktop_environment.desktop_file)):
                return desktop_environment
        return None

    @staticmethod
    def alsa_mixer_setup():
        """ Sets ALSA mixer settings """

        cmds = [
            "Master 70% unmute",
            "Front 70% unmute"
            "Side 70% unmute"
            "Surround 70% unmute",
            "Center 70% unmute",
            "LFE 70% unmute",
            "Headphone 70% unmute",
            "Speaker 70% unmute",
            "PCM 70% unmute",
            "Line 70% unmute",
            "External 70% unmute",
            "FM 50% unmute",
            "Master Mono 70% unmute",
            "Master Digital 70% unmute",
            "Analog Mix 70% unmute",
            "Aux 70% unmute",
            "Aux2 70% unmute",
            "PCM Center 70% unmute",
            "PCM Front 70% unmute",
            "PCM LFE 70% unmute",
            "PCM Side 70% unmute",
            "PCM Surround 70% unmute",
            "Playback 70% unmute",
            "PCM,1 70% unmute",
            "DAC 70% unmute",
            "DAC,0 70% unmute",
            "DAC,1 70% unmute",
            "Synth 70% unmute",
            "CD 70% unmute",
            "Wave 70% unmute",
            "Music 70% unmute",
            "AC97 70% unmute",
            "Analog Front 70% unmute",
            "VIA DXS,0 70% unmute",
            "VIA DXS,1 70% unmute",
            "VIA DXS,2 70% unmute",
            "VIA DXS,3 70% unmute",
            "Mic 70% mute",
            "IEC958 70% mute",
            "Master Playback Switch on",
            "Master Surround on",
            "SB Live Analog/Digital Output Jack off",
            "Audigy Analog/Digital Output Jack off"]

        for cmd in cmds:
            chroot_run(['sh', '-c', 'amixer -c 0 sset {0}'.format(cmd)])

        # Save settings
        chroot_run(['alsactl', '-f', '/etc/asound.state', 'store'])

    def set_autologin(self):
        """ Enables automatic login for the installed desktop manager """
        username = self.settings.get('username')
        self.queue_event('info', _("%s: Enable automatic login for user %s.") % (self.desktop_manager, username))

        if self.desktop_manager == 'mdm':
            # Systems with MDM as Desktop Manager
            mdm_conf_path = os.path.join(self.dest_dir, "etc/mdm/custom.conf")
            if os.path.exists(mdm_conf_path):
                with open(mdm_conf_path, "r") as mdm_conf:
                    text = mdm_conf.readlines()
                with open(mdm_conf_path, "w") as mdm_conf:
                    for line in text:
                        if '[daemon]' in line:
                            line = '[daemon]\nAutomaticLogin=%s\nAutomaticLoginEnable=True\n' % username
                        mdm_conf.write(line)
            else:
                with open(mdm_conf_path, "w") as mdm_conf:
                    mdm_conf.write('# Thus - Enable automatic login for user\n')
                    mdm_conf.write('[daemon]\n')
                    mdm_conf.write('AutomaticLogin=%s\n' % username)
                    mdm_conf.write('AutomaticLoginEnable=True\n')
        elif self.desktop_manager == 'gdm':
            # Systems with GDM as Desktop Manager
            gdm_conf_path = os.path.join(self.dest_dir, "etc/gdm/custom.conf")
            if os.path.exists(gdm_conf_path):
                with open(gdm_conf_path, "r") as gdm_conf:
                    text = gdm_conf.readlines()
                with open(gdm_conf_path, "w") as gdm_conf:
                    for line in text:
                        if '[daemon]' in line:
                            line = '[daemon]\nAutomaticLogin=%s\nAutomaticLoginEnable=True\n' % username
                        gdm_conf.write(line)
            else:
                with open(gdm_conf_path, "w") as gdm_conf:
                    gdm_conf.write('# Thus - Enable automatic login for user\n')
                    gdm_conf.write('[daemon]\n')
                    gdm_conf.write('AutomaticLogin=%s\n' % username)
                    gdm_conf.write('AutomaticLoginEnable=True\n')
        elif self.desktop_manager == 'kdm':
            # Systems with KDM as Desktop Manager
            kdm_conf_path = os.path.join(self.dest_dir, "usr/share/config/kdm/kdmrc")
            text = []
            with open(kdm_conf_path, "r") as kdm_conf:
                text = kdm_conf.readlines()
            with open(kdm_conf_path, "w") as kdm_conf:
                for line in text:
                    if '#AutoLoginEnable=true' in line:
                        line = 'AutoLoginEnable=true\n'
                    if 'AutoLoginUser=' in line:
                        line = 'AutoLoginUser=%s\n' % username
                    kdm_conf.write(line)
        elif self.desktop_manager == 'lxdm':
            # Systems with LXDM as Desktop Manager
            lxdm_conf_path = os.path.join(self.dest_dir, "etc/lxdm/lxdm.conf")
            text = []
            with open(lxdm_conf_path, "r") as lxdm_conf:
                text = lxdm_conf.readlines()
            with open(lxdm_conf_path, "w") as lxdm_conf:
                for line in text:
                    if '# autologin=dgod' in line:
                        line = 'autologin=%s\n' % username
                    lxdm_conf.write(line)
        elif self.desktop_manager == 'lightdm':
            # Systems with LightDM as Desktop Manager
            # Ideally, we should use configparser for the ini conf file,
            # but we just do a simple text replacement for now, as it worksforme(tm)
            lightdm_conf_path = os.path.join(self.dest_dir, "etc/lightdm/lightdm.conf")
            text = []
            with open(lightdm_conf_path, "r") as lightdm_conf:
                text = lightdm_conf.readlines()
            with open(lightdm_conf_path, "w") as lightdm_conf:
                for line in text:
                    if '#autologin-user=' in line:
                        line = 'autologin-user=%s\n' % username
                    lightdm_conf.write(line)
        elif self.desktop_manager == 'slim':
            # Systems with Slim as Desktop Manager
            slim_conf_path = os.path.join(self.dest_dir, "etc/slim.conf")
            text = []
            with open(slim_conf_path, "r") as slim_conf:
                text = slim_conf.readlines()
            with open(slim_conf_path, "w") as slim_conf:
                for line in text:
                    if 'auto_login' in line:
                        line = 'auto_login yes\n'
                    if 'default_user' in line:
                        line = 'default_user %s\n' % username
                    slim_conf.write(line)
        elif self.desktop_manager == 'sddm':
            # Systems with Sddm as Desktop Manager
            sddm_conf_path = os.path.join(self.dest_dir, "etc/sddm.conf")
            if os.path.isfile(sddm_conf_path):
                self.queue_event('info', "SDDM config file exists")
            else:
                chroot_run(["sh", "-c", "sddm --example-config > /etc/sddm.conf"])           
            text = []
            with open(sddm_conf_path, "r") as sddm_conf:
                text = sddm_conf.readlines()
            with open(sddm_conf_path, "w") as sddm_conf:
                for line in text:
                    # User= line, possibly commented out
                    if re.match('\\s*(?:#\\s*)?User=', line):
                        line = 'User={}\n'.format(username)
                    # Session= line, commented out or with empty value
                    if re.match('\\s*#\\s*Session=|\\s*Session=$', line):
                        default_desktop_environment = self.find_desktop_environment()
                        if default_desktop_environment != None:
                            line = 'Session={}.desktop\n'.format(default_desktop_environment.desktop_file)
                    sddm_conf.write(line)



    def configure_system(self):
        """ Final install steps
            Set clock, language, timezone
            Run mkinitcpio
            Populate pacman keyring
            Setup systemd services
            ... and more """

        self.queue_event('action', _("Configuring your new system"))

        self.auto_fstab()
        self.queue_event('debug', _('fstab file generated.'))

        # Copy configured networks in Live medium to target system
        if self.network_manager == 'NetworkManager':
            self.copy_network_config()

        self.queue_event('debug', _('Network configuration copied.'))

        self.queue_event("action", _("Configuring your new system"))
        self.queue_event('pulse')

        # enable services
        self.enable_services([self.network_manager])

        cups_service = os.path.join(self.dest_dir, "usr/lib/systemd/system/org.cups.cupsd.service")
        if os.path.exists(cups_service):
            self.enable_services(['org.cups.cupsd'])

        # enable targets
        self.enable_targets(['remote-fs.target'])
        
        self.queue_event('debug', 'Enabled installed services.')

        # Wait FOREVER until the user sets the timezone
        while self.settings.get('timezone_done') is False:
            # wait five seconds and try again
            time.sleep(5)

        if self.settings.get("use_ntp"):
            self.enable_services(["ntpd"])

        # Set timezone
        zoneinfo_path = os.path.join("/usr/share/zoneinfo", self.settings.get("timezone_zone"))
        chroot_run(['ln', '-s', zoneinfo_path, "/etc/localtime"])

        self.queue_event('debug', _('Time zone set.'))

        # Wait FOREVER until the user sets his params
        while self.settings.get('user_info_done') is False:
            # wait five seconds and try again
            time.sleep(5)

        # Set user parameters
        username = self.settings.get('username')
        fullname = self.settings.get('fullname')
        password = self.settings.get('password')
        root_password = self.settings.get('root_password')
        hostname = self.settings.get('hostname')

        sudoers_path = os.path.join(self.dest_dir, "etc/sudoers.d/10-installer")

        with open(sudoers_path, "w") as sudoers:
            sudoers.write('%s ALL=(ALL) ALL\n' % username)

        subprocess.check_call(["chmod", "440", sudoers_path])

        self.queue_event('debug', _('Sudo configuration for user %s done.') % username)

        default_groups = 'lp,video,network,storage,wheel,audio'

        if self.settings.get('require_password') is False:
            chroot_run(['groupadd', 'autologin'])
            default_groups += ',autologin'

        chroot_run(['useradd', '-m', '-s', '/bin/bash', '-g', 'users', '-G', default_groups, username])

        self.queue_event('debug', _('User %s added.') % username)

        self.change_user_password(username, password)

        chroot_run(['chfn', '-f', fullname, username])

        chroot_run(['chown', '-R', '%s:users' % username, "/home/%s" % username])

        hostname_path = os.path.join(self.dest_dir, "etc/hostname")
        with open(hostname_path, "w") as hostname_file:
            hostname_file.write(hostname)

        self.queue_event('debug', _('Hostname  %s set.') % hostname)

        # Set root password
        if not root_password is '':
            self.change_user_password('root', root_password)
            self.queue_event('debug', _('Set root password.'))
        else:
            self.change_user_password('root', password)
            self.queue_event('debug', _('Set the same password to root.'))

        # Generate locales
        keyboard_layout = self.settings.get("keyboard_layout")
        keyboard_variant = self.settings.get("keyboard_variant")
        locale = self.settings.get("locale")
        self.queue_event('info', _("Generating locales ..."))

        self.uncomment_locale_gen(locale)

        chroot_run(['locale-gen'])
        locale_conf_path = os.path.join(self.dest_dir, "etc/locale.conf")
        with open(locale_conf_path, "w") as locale_conf:
            locale_conf.write('LANG=%s\n' % locale)

        environment_path = os.path.join(self.dest_dir, "etc/environment")
        with open(environment_path, "w") as environment:
            environment.write('LANG=%s\n' % locale)

        # Set /etc/vconsole.conf
        vconsole_conf_path = os.path.join(self.dest_dir, "etc/vconsole.conf")
        with open(vconsole_conf_path, "w") as vconsole_conf:
            vconsole_conf.write('KEYMAP=%s\n' % keyboard_layout)

        self.queue_event('info', _("Adjusting hardware clock ..."))
        self.auto_timesetting()

        # Enter chroot system
        chroot_run_mount_special_dirs()

        # Install configs for root
        chroot_run(['cp', '-av', '/etc/skel/.', '/root/'])

        self.queue_event('info', _("Configuring hardware ..."))
        # Copy generated xorg.xonf to target
        if os.path.exists("/etc/X11/xorg.conf"):
            shutil.copy2('/etc/X11/xorg.conf',
                         os.path.join(self.dest_dir, 'etc/X11/xorg.conf'))

        # Configure ALSA
        self.alsa_mixer_setup()
        logging.debug(_("Updated Alsa mixer settings"))

        # Set pulse
        if os.path.exists(os.path.join(DEST_DIR, "usr/bin/pulseaudio-ctl")):
            chroot_run(['pulseaudio-ctl', 'normal'])

        # Install xf86-video driver
        if os.path.exists("/opt/livecd/pacman-gfx.conf"):
            self.queue_event('info', _("Installing drivers ..."))
            self.queue_event('pulse')
            mhwd_script_path = os.path.join(self.settings.get("thus"), "scripts", MHWD_SCRIPT)
            try:
                subprocess.check_call(["/usr/bin/bash", mhwd_script_path])
                self.queue_event('debug', "Finished installing drivers.")
            except subprocess.FileNotFoundError as e:
                txt = _("Can't execute the MHWD script")
                logging.error(txt)
                self.queue_fatal_event(txt)
                return False
            except subprocess.CalledProcessError as e:
                txt = "CalledProcessError.output = %s" % e.output
                logging.error(txt)
                self.queue_fatal_event(txt)
                return False

        self.queue_event('info', _("Configure display manager ..."))
        # Setup slim
        if os.path.exists("/usr/bin/slim"):
            self.desktop_manager = 'slim'

        # Setup sddm
        if os.path.exists("/usr/bin/sddm"):
            self.desktop_manager = 'sddm'

        # setup lightdm
        if os.path.exists("%s/usr/bin/lightdm" % self.dest_dir):
            default_desktop_environment = self.find_desktop_environment()
            if default_desktop_environment != None:
                os.system("sed -i -e 's/^.*user-session=.*/user-session=%s/' %s/etc/lightdm/lightdm.conf" % (default_desktop_environment.desktop_file, self.dest_dir))
                os.system("ln -s /usr/lib/lightdm/lightdm/gdmflexiserver %s/usr/bin/gdmflexiserver" % self.dest_dir)
            os.system("chmod +r %s/etc/lightdm/lightdm.conf" % self.dest_dir)
            self.desktop_manager = 'lightdm'

        # Setup gdm
        if os.path.exists("%s/usr/bin/gdm" % self.dest_dir):
            default_desktop_environment = self.find_desktop_environment()
            if default_desktop_environment != None:
                os.system("echo \"XSession=%s\" >> %s/var/lib/AccountsService/users/gdm" % (default_desktop_environment.desktop_file, self.dest_dir))
                os.system("echo \"Icon=\" >> %s/var/lib/AccountsService/users/gdm" % self.dest_dir)
            self.desktop_manager = 'gdm'

        # Setup mdm
        if os.path.exists("%s/usr/bin/mdm" % self.dest_dir):
            default_desktop_environment = self.find_desktop_environment()
            if default_desktop_environment != None:
                os.system("sed -i 's|default.desktop|%s.desktop|g' %s/etc/mdm/custom.conf" % (default_desktop_environment.desktop_file, self.dest_dir))
            self.desktop_manager = 'mdm'

        # Setup lxdm
        if os.path.exists("%s/usr/bin/lxdm" % self.dest_dir):
            default_desktop_environment = self.find_desktop_environment()
            if default_desktop_environment != None:
                os.system("sed -i -e 's|^.*session=.*|session=%s|' %s/etc/lxdm/lxdm.conf" % (default_desktop_environment.executable, self.dest_dir))
            self.desktop_manager = 'lxdm'

        # Setup kdm
        if os.path.exists("%s/usr/bin/kdm" % self.dest_dir):
            self.desktop_manager = 'kdm'

        self.queue_event('info', _("Configure System ..."))

        # Add BROWSER var
        os.system("echo \"BROWSER=/usr/bin/xdg-open\" >> %s/etc/environment" % self.dest_dir)
        os.system("echo \"BROWSER=/usr/bin/xdg-open\" >> %s/etc/skel/.bashrc" % self.dest_dir)
        os.system("echo \"BROWSER=/usr/bin/xdg-open\" >> %s/etc/profile" % self.dest_dir)
        # Add TERM var
        if os.path.exists("%s/usr/bin/mate-session" % self.dest_dir):
            os.system("echo \"TERM=mate-terminal\" >> %s/etc/environment" % self.dest_dir)
            os.system("echo \"TERM=mate-terminal\" >> %s/etc/profile" % self.dest_dir)

        # Adjust Steam-Native when libudev.so.0 is available
        if os.path.exists("%s/usr/lib/libudev.so.0" % self.dest_dir) or os.path.exists("%s/usr/lib32/libudev.so.0" % self.dest_dir):
            os.system("echo -e \"STEAM_RUNTIME=0\nSTEAM_FRAME_FORCE_CLOSE=1\" >> %s/etc/environment" % self.dest_dir)

        # Remove thus
        if os.path.exists("%s/usr/bin/thus" % self.dest_dir):
            self.queue_event('info', _("Removing live configuration (packages)"))
            chroot_run(['pacman', '-R', '--noconfirm', 'thus'])

        # Remove virtualbox driver on real hardware
        p1 = subprocess.Popen(["mhwd"], stdout=subprocess.PIPE)
        p2 = subprocess.Popen(["grep", "0300:80ee:beef"], stdin=p1.stdout, stdout=subprocess.PIPE)
        num_res = p2.communicate()[0]
        if num_res == "0":
            chroot_run(['sh', '-c', 'pacman -Rsc --noconfirm $(pacman -Qq | grep virtualbox-guest-modules)'])

        # Set unique machine-id
        chroot_run(['dbus-uuidgen', '--ensure=/etc/machine-id'])
        chroot_run(['dbus-uuidgen', '--ensure=/var/lib/dbus/machine-id'])


        # Setup pacman
        self.queue_event("action", _("Configuring package manager"))
        self.queue_event("pulse")

        # Copy mirror list
        shutil.copy2('/etc/pacman.d/mirrorlist',
                     os.path.join(self.dest_dir, 'etc/pacman.d/mirrorlist'))

        # Copy random generated keys by pacman-init to target
        if os.path.exists("%s/etc/pacman.d/gnupg" % self.dest_dir):
            os.system("rm -rf %s/etc/pacman.d/gnupg" % self.dest_dir)
        os.system("cp -a /etc/pacman.d/gnupg %s/etc/pacman.d/" % self.dest_dir)
        chroot_run(['pacman-key', '--populate', 'archlinux', 'manjaro'])
        self.queue_event('info', _("Finished configuring package manager."))

        if os.path.exists("%s/etc/keyboard.conf" % self.dest_dir):
            consolefh = open("%s/etc/keyboard.conf" % self.dest_dir, "r")
            newconsolefh = open("%s/etc/keyboard.new" % self.dest_dir, "w")
            for line in consolefh:
                 line = line.rstrip("\r\n")
                 if(line.startswith("XKBLAYOUT=")):
                     newconsolefh.write("XKBLAYOUT=\"%s\"\n" % keyboard_layout)
                 elif(line.startswith("XKBVARIANT=") and keyboard_variant != ''):
                     newconsolefh.write("XKBVARIANT=\"%s\"\n" % keyboard_variant)
                 else:
                     newconsolefh.write("%s\n" % line)
            consolefh.close()
            newconsolefh.close()
            chroot_run(['mv', '/etc/keyboard.conf', '/etc/keyboard.conf.old'])
            chroot_run(['mv', '/etc/keyboard.new', '/etc/keyboard.conf'])
        else:
            keyboardconf = open("%s/etc/X11/xorg.conf.d/00-keyboard.conf" % self.dest_dir, "w")
            keyboardconf.write("\n");
            keyboardconf.write("Section \"InputClass\"\n")
            keyboardconf.write(" Identifier \"system-keyboard\"\n") 
            keyboardconf.write(" MatchIsKeyboard \"on\"\n")
            keyboardconf.write(" Option \"XkbLayout\" \"%s\"\n" % keyboard_layout)
            keyboardconf.write(" Option \"XkbModel\" \"%s\"\n" % "pc105")
            keyboardconf.write(" Option \"XkbVariant\" \"%s\"\n" % keyboard_variant)
            keyboardconf.write(" Option \"XkbOptions\" \"%s\"\n" % "terminate:ctrl_alt_bksp")        
            keyboardconf.write("EndSection\n")
            keyboardconf.close()

        # Let's start without using hwdetect for mkinitcpio.conf.
        # I think it should work out of the box most of the time.
        # This way we don't have to fix deprecated hooks.
        # NOTE: With LUKS or LVM maybe we'll have to fix deprecated hooks.
        self.queue_event('info', _("Running mkinitcpio ..."))
        self.queue_event("pulse")
        mkinitcpio.run(DEST_DIR, self.settings, self.mount_devices, self.blvm)
        self.queue_event('info', _("Running mkinitcpio - done"))

        # Set autologin if selected
        # Warning: In openbox "desktop", the post-install script writes /etc/slim.conf
        # so we always have to call set_autologin AFTER the post-install script call.
        if self.settings.get('require_password') is False:
            self.set_autologin()

        # Encrypt user's home directory if requested (NOT FINISHED YET)
        if self.settings.get('encrypt_home'):
            self.queue_event('debug', _("Encrypting user home dir ..."))
            encfs.setup(username, self.dest_dir)
            self.queue_event('debug', _("User home dir encrypted"))
