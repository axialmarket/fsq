# fsq -- a python library for manipulating and introspecting FSQ queues
# @author: Matthew Story <matt.story@axialmarket.com>
#
# fsq/enqueue.py -- provides enqueueing functions: enqueue, senqueue,
#                   venqueue, vsenqueue
#
#     fsq is all unicode internally, if you pass in strings,
#     they will be explicitly coerced to unicode.
#
# This software is for POSIX compliant systems only.
import errno
import os
import socket
import datetime
from cStringIO import StringIO
from contextlib import closing

from . import FSQInternalError, FSQTimeFmtError, FSQEnqueueError,\
              FSQMaxEnqueueTriesError, FSQ_DELIMITER, FSQ_TIMEFMT, FSQ_QUEUE,\
              FSQ_TMP, FSQ_ROOT, FSQ_ENCODE, FSQ_USER, FSQ_GROUP, FSQ_MODE,\
              FSQ_ENQUEUE_TRIES, path as fsq_path, construct
from .internal import uid_gid, rationalize_file

####### INTERNAL MODULE FUNCTIONS AND ATTRIBUTES #######
# these keyword arguments are supported, but don't have defaults
# they are just passed along to _standard_args, having this argument
# list repeated in 2 places is non-ideal
_STANDARD_KEYS = ('entropy', 'tries', 'pid', 'now', 'hostname',)

# get the standard arguments that we always send
def _std_args(entropy=0, tries=0, pid=os.getpid(), timefmt=FSQ_TIMEFMT,
                   now=None, hostname=socket.gethostname()):
    '''Provide the arguments which are always required by FSQ spec for
       uniqueness time-based ordering and environment, returns a list:

           [
               now,      # formatted by timefmt (default FSQ_TIMEFMT)
               entropy,  # for uniqueness, (default 0)
               pid,      # default is pid of the calling process
               hostname, # default is this machine's hostname
               tries,    # number of times this work has been attempted
           ]             # (default 0)
    '''
    if now is None:
        now = datetime.datetime.now()
    try:
        fmt_time = now.strftime(timefmt)
    except AttributeError, e:
        raise FSQInternalError(errno.EINVAL, u'now must be a datetime,'\
                               u' date, time, or other type supporting'\
                               u' strftime, not {0}'.format(
                               now.__class__.__name__))
    except TypeError, e:
        raise TypeError(u'timefmt must be a string or read-only buffer,'\
                        ' not {0}'.format(timefmt.__class__.__name__))
    except ValueError, e:
        raise FSQTimeFmtError(errno.EINVAL, u'invalid fmt for strftime:'\
                              ' {0}'.format(timefmt))
    try:
        return [ unicode(fmt_time), unicode(entropy), unicode(pid),
                 unicode(hostname), unicode(tries) ]
    except (UnicodeDecodeError, UnicodeEncodeError, ), e:
        raise FSQInternalError(errno.EINVAL, e.message)

# TODO: provide an internal/external streamable queue item object use that
#       instead of this for the enqueue family of functions
# make a queue item from args, return a file
def _mkitem(trg_path, args, user=FSQ_USER, group=FSQ_GROUP, mode=FSQ_MODE,
            entropy=None, enqueue_tries=FSQ_ENQUEUE_TRIES,
            delimiter=FSQ_DELIMITER, encodeseq=FSQ_ENCODE, **std_kwargs):
    trg_fd = trg = name = None
    tried = 0
    recv_entropy = True if std_kwargs.has_key('entropy') else False
    now, entropy, pid, host, tries = _std_args(**std_kwargs)
    # try a few times
    while 0 >= enqueue_tries or enqueue_tries > tried:
        tried += 1
        # get low, so we can use some handy options; man 2 open
        try:
            name = construct((now, entropy, pid, host, tries, ) + tuple(args),
                             delimiter=delimiter, encodeseq=encodeseq)
            trg = os.path.join(trg_path, name)
            trg_fd = os.open(trg, os.O_CREAT|os.O_EXCL|os.O_WRONLY, mode)
        except (OSError, IOError, ), e:
            # if file already exists, retry or break
            if e.errno == errno.EEXIST:
                if recv_entropy:
                    break
                entropy = tried
                continue

            # re-raise
            raise e
        try:
            # set user/group ownership for file; man 2 fchown
            os.fchown(trg_fd, *uid_gid(user, group))
            # return something that is safe to close in scope
            return trg, os.fdopen(os.dup(trg_fd), 'wb', 1)
        except Exception, e:
            os.unlink(trg)
            raise e
        # we return a file on a dup'ed fd, always close original fd
        finally:
            os.close(trg_fd)

    # if we got nowhere ... raise
    raise FSQMaxEnqueueTriesError(errno.EAGAIN, u'max tries exhausted for:'\
                                  u' {0}'.format(trg))

####### EXPOSED METHODS #######
def enqueue(trg_queue, item_f, *args, **kwargs):
    '''Enqueue the contents of a file, or file-like object, file-descriptor or
       the contents of a file at an address (e.g. '/my/file') queue with
       arbitrary arguments, enqueue is to venqueue what printf is to vprintf
    '''
    return venqueue(trg_queue, item_f, args, **kwargs)

def senqueue(trg_queue, item_s, *args, **kwargs):
    '''Enqueue a string, or string-like object to queue with arbitrary
       arguments, senqueue is to enqueue what sprintf is to printf, senqueue
       is to vsenqueue what sprintf is to vsprintf.
    '''
    return vsenqueue(trg_queue, item_s, args, **kwargs)

def venqueue(trg_queue, item_f, args, delimiter=FSQ_DELIMITER,
             encodeseq=FSQ_ENCODE, timefmt=FSQ_TIMEFMT, queue=FSQ_QUEUE,
             tmp=FSQ_TMP, root=FSQ_ROOT, user=FSQ_USER, group=FSQ_GROUP,
             mode=FSQ_MODE, enqueue_tries=FSQ_ENQUEUE_TRIES, **kwargs):
    '''Enqueue the contents of a file, or file-like object, file-descriptor or
       the contents of a file at an address (e.g. '/my/file') queue with
       an argument list, venqueue is to enqueue what vprintf is to printf

       If entropy is passed in, failure on duplicates is raised to the caller,
       if entropy is not passed in, venqueue will increment entropy until it
       can create the queue item.
    '''
    # grab only the kwargs we want for _mkitem
    std_kwargs = {}
    for k in _STANDARD_KEYS:
        try:
            std_kwargs[k] = kwargs[k]
            del kwargs[k]
        except KeyError:
            pass
    if kwargs.keys():
        raise TypeError(u'venqueue() got an unexpected keyword argument:'\
                        u' {0}'.format(kwargs.keys()[0]))
    # open source file
    with closing(rationalize_file(item_f)) as src_file:
        tmp_path = fsq_path.tmp(trg_queue, root=root, tmp=tmp)
        queue_path = fsq_path.queue(trg_queue, root=root, queue=queue)
        # yeild temporary queue item
        name, trg_file = _mkitem(tmp_path, args, user=user, group=group,
                                 mode=mode, delimiter=delimiter,
                                 encodeseq=encodeseq, **std_kwargs)
        with closing(trg_file):
            try:
                # i/o time ...
                while True:
                    line = src_file.readline()
                    if not line:
                        break
                    trg_file.write(line)

                # rename will overwrite trg, so open trg with O_EXCL first
                commit_name, commit_file = _mkitem(queue_path, args,
                                                   **std_kwargs)
                with closing(commit_file):
                    os.rename(name, commit_name)
            except Exception, e:
                try:
                    os.unlink(name)
                except OSError, e:
                    if e.errno != errno.ENOENT:
                       raise e
                raise e
            finally:
                pass

def vsenqueue(trg_queue, item_s, args, **kwargs):
    '''Enqueue a string, or string-like object to queue with arbitrary
       arguments, vsenqueue is to venqueue what vsprintf is to vprintf,
       vsenqueue is to senqueue what vsprintf is to sprintf.
    '''
    return venqueue(trg_queue, StringIO(item_s), args, **kwargs)