# -*- coding: utf-8 -*-
'''
States to manage git repositories and git configuration

.. important::
    Before using git over ssh, make sure your remote host fingerprint exists in
    your ``~/.ssh/known_hosts`` file.
'''
from __future__ import absolute_import

# Import python libs
import copy
import logging
import os
import re
import string
from distutils.version import LooseVersion as _LooseVersion

# Import salt libs
import salt.utils
import salt.utils.url
from salt.exceptions import CommandExecutionError
from salt.ext import six

log = logging.getLogger(__name__)


def __virtual__():
    '''
    Only load if git is available
    '''
    return __salt__['cmd.has_exec']('git')


def _revs_equal(rev1, rev2, rev_type):
    '''
    Shorthand helper function for comparing SHA1s. If rev_type == 'sha1' then
    the comparison will be done using str.startwith() to allow short SHA1s to
    compare successfully.

    NOTE: This means that rev2 must be the short rev.
    '''
    if (rev1 is None and rev2 is not None) \
            or (rev2 is None and rev1 is not None):
        return False
    elif rev1 is rev2 is None:
        return True
    elif rev_type == 'sha1':
        return rev1.startswith(rev2)
    else:
        return rev1 == rev2


def _short_sha(sha1):
    return sha1[:7] if sha1 is not None else None


def _format_comments(comments):
    '''
    Return a joined list
    '''
    ret = '. '.join(comments)
    if len(comments) > 1:
        ret += '.'
    return ret


def _parse_fetch(output):
    '''
    Go through the output from a git fetch and return a dict
    '''
    update_re = re.compile(
        r'.*(?:([0-9a-f]+)\.\.([0-9a-f]+)|'
        r'\[(?:new (tag|branch)|tag update)\])\s+(.+)->'
    )
    ret = {}
    for line in output.splitlines():
        match = update_re.match(line)
        if match:
            old_sha, new_sha, new_ref_type, ref_name = \
                match.groups()
            ref_name = ref_name.rstrip()
            if new_ref_type is not None:
                # ref is a new tag/branch
                ref_key = 'new tags' \
                    if new_ref_type == 'tag' \
                    else 'new branches'
                ret.setdefault(ref_key, []).append(ref_name)
            elif old_sha is not None:
                # ref is a branch update
                ret.setdefault('updated_branches', {})[ref_name] = \
                    {'old': old_sha, 'new': new_sha}
            else:
                # ref is an updated tag
                ret.setdefault('updated tags', []).append(ref_name)
    return ret


def _get_local_rev_and_branch(target, user):
    '''
    Return the local revision for before/after comparisons
    '''
    log.info('Checking local revision for {0}'.format(target))
    try:
        local_rev = __salt__['git.revision'](target,
                                             user=user,
                                             ignore_retcode=True)
    except CommandExecutionError:
        log.info('No local revision for {0}'.format(target))
        local_rev = None

    log.info('Checking local branch for {0}'.format(target))
    try:
        local_branch = __salt__['git.current_branch'](target,
                                                      user=user,
                                                      ignore_retcode=True)
    except CommandExecutionError:
        log.info('No local branch for {0}'.format(target))
        local_branch = None

    return local_rev, local_branch


def _strip_exc(exc):
    '''
    Strip the actual command that was run from exc.strerror to leave just the
    error message
    '''
    return re.sub(r'^Command [\'"].+[\'"] failed: ', '', exc.strerror)


def _uptodate(ret, target, comments=None):
    ret['comment'] = 'Repository {0} is up-to-date'.format(target)
    if comments:
        # Shouldn't be making any changes if the repo was up to date, but
        # report on them so we are alerted to potential problems with our
        # logic.
        ret['comment'] += '\n\nChanges made: '
        ret['comment'] += _format_comments(comments)
    return ret


def _neutral_test(ret, comment):
    ret['result'] = None
    ret['comment'] = comment
    return ret


def _fail(ret, msg, comments=None):
    ret['result'] = False
    if comments:
        msg += '\n\nChanges already made: '
        msg += _format_comments(comments)
    ret['comment'] = msg
    return ret


def _not_fast_forward(ret, pre, post, branch, local_branch, comments):
    return _fail(
        ret,
        'Repository would be updated from {0} to {1}{2}, but this is not a '
        'fast-forward merge. Set \'force_reset\' to True to force this '
        'update.'.format(
            _short_sha(pre),
            _short_sha(post),
            ' (after checking out local branch \'{0}\')'.format(branch)
                if branch is not None and branch != local_branch
                else ''
        ),
        comments
    )


def latest(name,
           rev='HEAD',
           target=None,
           branch=None,
           user=None,
           force_checkout=False,
           force_clone=False,
           force_fetch=False,
           force_reset=False,
           submodules=False,
           bare=False,
           mirror=False,
           remote='origin',
           fetch_tags=True,
           depth=None,
           identity=None,
           https_user=None,
           https_pass=None,
           onlyif=False,
           unless=False,
           **kwargs):
    '''
    Make sure the repository is cloned to the given directory and is
    up-to-date.

    name
        Address of the remote repository as passed to "git clone"

    rev : HEAD
        The remote branch, tag, or revision ID to checkout after clone / before
        update. If specified, then Salt will also ensure that the tracking
        branch is set to ``<remote>/<rev>``, unless ``rev`` refers to a tag or
        SHA1, in which case Salt will ensure that the tracking branch is unset.

        If ``rev`` is not specified, it will be assumed to be ``HEAD``, and
        Salt will not manage the tracking branch at all.

    target
        Name of the target directory where repository is about to be cloned

    branch
        Name of the branch into which to checkout the specified rev. If not
        specified, then Salt will not care what branch is being used locally
        and will just use whatever branch is currently there.

        .. note::
            If not specified, this means that the local branch name will not be
            changed if the repository is reset to another branch/tag/SHA1.

        .. versionadded:: 2015.8.0

    user
        User under which to run git commands. By default, commands are run by
        the user under which the minion is running.

        .. versionadded:: 0.17.0

    force : False
        .. deprecated:: 2015.8.0
            Use ``force_clone`` instead. For earlier Salt versions, ``force``
            must be used.

    force_checkout : False
        When checking out the local branch, the state will fail if there are
        unwritten changes. Set this argument to ``True`` to discard unwritten
        changes when checking out.

    force_clone : False
        If the ``target`` directory exists and is not a git repository, then
        this state will fail. Set this argument to ``True`` to remove the
        contents of the target directory and clone the repo into it.

    force_fetch : False
        If a fetch needs to be performed, non-fast-forward fetches will cause
        this state to fail. Set this argument to ``True`` to force the fetch
        even if it is a non-fast-forward update.

        .. versionadded:: 2015.8.0

    force_reset : False
        If the update is not a fast-forward, this state will fail. Set this
        argument to ``True`` to force a hard-reset to the remote revision in
        these cases.

    submodules : False
        Update submodules on clone or branch change

    bare : False
        Set to ``True`` if the repository is to be a bare clone of the remote
        repository.

        .. note:

            Setting this option to ``True`` is incompatible with the ``rev``
            argument.

    mirror
        Set to ``True`` if the repository is to be a mirror of the remote
        repository. This implies that ``bare`` set to ``True``, and thus is
        incompatible with ``rev``.

    remote : origin
        Git remote to use. If this state needs to clone the repo, it will clone
        it using this value as the initial remote name. If the repository
        already exists, and a remote by this name is not present, one will be
        added.

    remote_name
        .. deprecated:: 2015.8.0
            Use ``remote`` instead. For earlier Salt versions, ``remote_name``
            must be used.

    fetch_tags : True
        If ``True``, then when a fetch is performed all tags will be fetched,
        even those which are not reachable by any branch on the remote.

    depth
        Defines depth in history when git a clone is needed in order to ensure
        latest. E.g. ``depth: 1`` is usefull when deploying from a repository
        with a long history. Use rev to specify branch. This is not compatible
        with tags or revision IDs.

    identity
        A path on the minion server to a private key to use over SSH

        Key can be specified as a SaltStack file server URL, eg. salt://location/identity_file

        .. versionadded:: Boron

    https_user
        HTTP Basic Auth username for HTTPS (only) clones

        .. versionadded:: 2015.5.0

    https_pass
        HTTP Basic Auth password for HTTPS (only) clones

        .. versionadded:: 2015.5.0

    onlyif
        A command to run as a check, run the named command only if the command
        passed to the ``onlyif`` option returns true

    unless
        A command to run as a check, only run the named command if the command
        passed to the ``unless`` option returns false

    .. note::
        Clashing ID declarations can be avoided when including different
        branches from the same git repository in the same sls file by using the
        ``name`` declaration.  The example below checks out the ``gh-pages``
        and ``gh-pages-prod`` branches from the same repository into separate
        directories.  The example also sets up the ``ssh_known_hosts`` ssh key
        required to perform the git checkout.

    .. code-block:: yaml

        gitlab.example.com:
          ssh_known_hosts:
            - present
            - user: root
            - enc: ecdsa
            - fingerprint: 4e:94:b0:54:c1:5b:29:a2:70:0e:e1:a3:51:ee:ee:e3

        git-website-staging:
          git.latest:
            - name: ssh://git@gitlab.example.com:user/website.git
            - rev: gh-pages
            - target: /usr/share/nginx/staging
            - identity: /root/.ssh/website_id_rsa
            - require:
              - pkg: git
              - ssh_known_hosts: gitlab.example.com

        git-website-staging:
          git.latest:
            - name: ssh://git@gitlab.example.com:user/website.git
            - rev: gh-pages
            - target: /usr/share/nginx/staging
            - identity: salt://website/id_rsa
            - require:
              - pkg: git
              - ssh_known_hosts: gitlab.example.com

            .. versionadded:: Boron

        git-website-prod:
          git.latest:
            - name: ssh://git@gitlab.example.com:user/website.git
            - rev: gh-pages-prod
            - target: /usr/share/nginx/prod
            - identity: /root/.ssh/website_id_rsa
            - require:
              - pkg: git
              - ssh_known_hosts: gitlab.example.com
    '''
    ret = {'name': name, 'result': True, 'comment': '', 'changes': {}}

    kwargs = salt.utils.clean_kwargs(**kwargs)
    always_fetch = kwargs.pop('always_fetch', False)
    force = kwargs.pop('force', False)
    remote_name = kwargs.pop('remote_name', False)
    if kwargs:
        return _fail(
            ret,
            salt.utils.invalid_kwargs(kwargs, raise_exc=False)
        )

    if always_fetch:
        salt.utils.warn_until(
            'Nitrogen',
            'The \'always_fetch\' argument to the git.latest state no longer '
            'has any effect, see the 2015.8.0 release notes for details.'
        )
    if force:
        salt.utils.warn_until(
            'Nitrogen',
            'The \'force\' argument to the git.latest state has been '
            'deprecated, please use \'force_clone\' instead.'
        )
        force_clone = force
    if remote_name:
        salt.utils.warn_until(
            'Nitrogen',
            'The \'remote_name\' argument to the git.latest state has been '
            'deprecated, please use \'remote\' instead.'
        )
        remote = remote_name

    if not remote:
        return _fail(ret, '\'remote\' argument is required')

    if not target:
        return _fail(ret, '\'target\' argument is required')

    if not rev:
        return _fail(
            ret,
            '\'{0}\' is not a valid value for the \'rev\' argument'.format(rev)
        )

    # Ensure that certain arguments are strings to ensure that comparisons work
    if not isinstance(rev, six.string_types):
        rev = str(rev)
    if target is not None:
        if not isinstance(target, six.string_types):
            target = str(target)
        if not os.path.isabs(target):
            return _fail(
                ret,
                'target \'{0}\' is not an absolute path'.format(target)
            )
    if branch is not None and not isinstance(branch, six.string_types):
        branch = str(branch)
    if user is not None and not isinstance(user, six.string_types):
        user = str(user)
    if remote is not None and not isinstance(remote, six.string_types):
        remote = str(remote)
    if identity is not None:
        if isinstance(identity, six.string_types):
            identity = [identity]
        elif not isinstance(identity, list):
            return _fail(ret, 'identity must be either a list or a string')
        for ident_path in identity:
            if 'salt://' in ident_path:
                try:
                    ident_path = __salt__['cp.cache_file'](ident_path)
                except IOError as exc:
                    log.error(
                        'Failed to cache {0}: {1}'.format(ident_path, exc)
                    )
                    return _fail(
                        ret,
                        'identity \'{0}\' does not exist.'.format(
                            ident_path
                        )
                    )
            if not os.path.isabs(ident_path):
                return _fail(
                    ret,
                    'identity \'{0}\' is not an absolute path'.format(
                        ident_path
                    )
                )
    if https_user is not None and not isinstance(https_user, six.string_types):
        https_user = str(https_user)
    if https_pass is not None and not isinstance(https_pass, six.string_types):
        https_pass = str(https_pass)

    if os.path.isfile(target):
        return _fail(
            ret,
            'Target \'{0}\' exists and is a regular file, cannot proceed'
            .format(target)
        )

    try:
        desired_fetch_url = salt.utils.url.add_http_basic_auth(
            name,
            https_user,
            https_pass,
            https_only=True
        )
    except ValueError as exc:
        return _fail(ret, exc.__str__())

    redacted_fetch_url = \
        salt.utils.url.redact_http_basic_auth(desired_fetch_url)

    if mirror:
        bare = True

    # Check to make sure rev and mirror/bare are not both in use
    if rev != 'HEAD' and bare:
        return _fail(ret, ('\'rev\' is not compatible with the \'mirror\' and '
                           '\'bare\' arguments'))

    run_check_cmd_kwargs = {'runas': user}
    if 'shell' in __grains__:
        run_check_cmd_kwargs['shell'] = __grains__['shell']

    # check if git.latest should be applied
    cret = mod_run_check(
        run_check_cmd_kwargs, onlyif, unless
    )
    if isinstance(cret, dict):
        ret.update(cret)
        return ret

    refspecs = [
        'refs/heads/*:refs/remotes/{0}/*'.format(remote),
        '+refs/tags/*:refs/tags/*'
    ] if fetch_tags else []

    log.info('Checking remote revision for {0}'.format(name))
    try:
        all_remote_refs = __salt__['git.remote_refs'](
            name,
            heads=False,
            tags=False,
            user=user,
            identity=identity,
            https_user=https_user,
            https_pass=https_pass,
            ignore_retcode=False)
    except CommandExecutionError as exc:
        return _fail(
            ret,
            'Failed to check remote refs: {0}'.format(_strip_exc(exc))
        )

    if bare:
        remote_rev = None
    else:
        if rev == 'HEAD':
            if 'HEAD' in all_remote_refs:
                # head_ref will only be defined if rev == 'HEAD', be careful
                # how this is used below
                head_ref = remote + '/HEAD'
                remote_rev = all_remote_refs['HEAD']
                # Just go with whatever the upstream currently is
                desired_upstream = None
                remote_rev_type = 'sha1'
            else:
                # Empty remote repo
                remote_rev = None
                remote_rev_type = None
        elif 'refs/heads/' + rev in all_remote_refs:
            remote_rev = all_remote_refs['refs/heads/' + rev]
            desired_upstream = '/'.join((remote, rev))
            remote_rev_type = 'branch'
        elif 'refs/tags/' + rev + '^{}' in all_remote_refs:
            # Annotated tag
            remote_rev = all_remote_refs['refs/tags/' + rev + '^{}']
            desired_upstream = False
            remote_rev_type = 'tag'
        elif 'refs/tags/' + rev in all_remote_refs:
            # Non-annotated tag
            remote_rev = all_remote_refs['refs/tags/' + rev]
            desired_upstream = False
            remote_rev_type = 'tag'
        else:
            if len(rev) <= 40 \
                    and all(x in string.hexdigits for x in rev):
                # git ls-remote did not find the rev, and because it's a
                # hex string <= 40 chars we're going to assume that the
                # desired rev is a SHA1
                rev = rev.lower()
                remote_rev = rev
                desired_upstream = False
                remote_rev_type = 'sha1'
            else:
                remote_rev = None

    if remote_rev is None and not bare:
        if rev != 'HEAD':
            # A specific rev is desired, but that rev doesn't exist on the
            # remote repo.
            return _fail(
                ret,
                'No revision matching \'{0}\' exists in the remote '
                'repository'.format(rev)
            )

    git_ver = _LooseVersion(__salt__['git.version'](versioninfo=False))
    if git_ver >= _LooseVersion('1.8.0'):
        set_upstream = '--set-upstream-to'
    else:
        # Older git uses --track instead of --set-upstream-to
        set_upstream = '--track'

    check = 'refs' if bare else '.git'
    gitdir = os.path.join(target, check)
    comments = []
    if os.path.isdir(gitdir) or __salt__['git.is_worktree'](target):
        # Target directory is a git repository or git worktree
        try:
            all_local_branches = __salt__['git.list_branches'](
                target, user=user)
            all_local_tags = __salt__['git.list_tags'](target, user=user)
            local_rev, local_branch = _get_local_rev_and_branch(target, user)

            if remote_rev is None and local_rev is not None:
                return _fail(
                    ret,
                    'Remote repository is empty, cannot update from a '
                    'non-empty to an empty repository'
                )

            # Base rev and branch are the ones from which any reset or merge
            # will take place. If the branch is not being specified, the base
            # will be the "local" rev and branch, i.e. those we began with
            # before this state was run. If a branch is being specified and it
            # both exists and is not the one with which we started, then we'll
            # be checking that branch out first, and it instead becomes our
            # base. The base branch and rev will be used below in comparisons
            # to determine what changes to make.
            base_rev = local_rev
            base_branch = local_branch
            if branch is not None and branch != local_branch:
                if branch in all_local_branches:
                    base_branch = branch
                    # Desired branch exists locally and is not the current
                    # branch. We'll be performing a checkout to that branch
                    # eventually, but before we do that we need to find the
                    # current SHA1
                    try:
                        base_rev = __salt__['git.rev_parse'](
                            target,
                            branch + '^{commit}',
                            ignore_retcode=True)
                    except CommandExecutionError as exc:
                        return _fail(
                            ret,
                            'Unable to get position of local branch \'{0}\': '
                            '{1}'.format(branch, _strip_exc(exc)),
                            comments
                        )

            remotes = __salt__['git.remotes'](target,
                                              user=user,
                                              redact_auth=False)

            if remote_rev_type == 'sha1' \
                    and base_rev is not None \
                    and base_rev.startswith(remote_rev):
                # Either we're already checked out to the branch we need and it
                # is up-to-date, or the branch to which we need to switch is
                # on the same SHA1 as the desired remote revision. Either way,
                # we know we have the remote rev present already and no fetch
                # will be needed.
                has_remote_rev = True
            else:
                has_remote_rev = False
                if remote_rev is not None:
                    try:
                        __salt__['git.rev_parse'](
                            target,
                            remote_rev + '^{commit}',
                            ignore_retcode=True)
                    except CommandExecutionError:
                        # Local checkout doesn't have the remote_rev
                        pass
                    else:
                        # The object might exist enough to get a rev-parse to
                        # work, while the local ref could have been
                        # deleted/changed/force updated. Do some further sanity
                        # checks to determine if we really do have the
                        # remote_rev.
                        if remote_rev_type == 'branch':
                            if remote in remotes:
                                try:
                                    # Do a rev-parse on <remote>/<rev> to get
                                    # the local SHA1 for it, so we can compare
                                    # it to the remote_rev SHA1.
                                    local_copy = __salt__['git.rev_parse'](
                                        target,
                                        desired_upstream,
                                        ignore_retcode=True)
                                except CommandExecutionError:
                                    pass
                                else:
                                    # If the SHA1s don't match, then the remote
                                    # branch was force-updated, and we need to
                                    # fetch to update our local copy the ref
                                    # for the remote branch. If they do match,
                                    # then we have the remote_rev and don't
                                    # need to fetch.
                                    if local_copy == remote_rev:
                                        has_remote_rev = True
                        elif remote_rev_type == 'tag':
                            if rev in all_local_tags:
                                try:
                                    local_tag_sha1 = __salt__['git.rev_parse'](
                                        target,
                                        rev + '^{commit}',
                                        ignore_retcode=True)
                                except CommandExecutionError:
                                    # Shouldn't happen if the tag exists
                                    # locally but account for this just in
                                    # case.
                                    local_tag_sha1 = None
                                if local_tag_sha1 == remote_rev:
                                    has_remote_rev = True
                                else:
                                    if not force_reset:
                                        # SHA1 of tag on remote repo is
                                        # different than local tag. Unless
                                        # we're doing a hard reset then we
                                        # don't need to proceed as we know that
                                        # the fetch will update the tag and the
                                        # only way to make the state succeed is
                                        # to reset the branch to point at the
                                        # tag's new location.
                                        return _fail(
                                            ret,
                                            '\'{0}\' is a tag, but the remote '
                                            'SHA1 for this tag ({1}) doesn\'t '
                                            'match the local SHA1 ({2}). Set '
                                            '\'force_reset\' to True to force '
                                            'this update.'.format(
                                                rev,
                                                _short_sha(remote_rev),
                                                _short_sha(local_tag_sha1)
                                            )
                                        )
                        elif remote_rev_type == 'sha1':
                            has_remote_rev = True

            if not has_remote_rev:
                # Either the remote rev could not be found with git
                # ls-remote (in which case we won't know more until
                # fetching) or we're going to be checking out a new branch
                # and don't have to worry about fast-forwarding.
                fast_forward = None
            else:
                if base_rev is None:
                    # If we're here, the remote_rev exists in the local
                    # checkout but there is still no HEAD locally. A possible
                    # reason for this is that an empty repository existed there
                    # and a remote was added and fetched, but the repository
                    # was not fast-forwarded. Regardless, going from no HEAD to
                    # a locally-present rev is considered a fast-forward update.
                    fast_forward = True
                else:
                    fast_forward = __salt__['git.merge_base'](
                        target,
                        refs=[base_rev, remote_rev],
                        is_ancestor=True,
                        user=user,
                        ignore_retcode=True)

            if fast_forward is False:
                if not force_reset:
                    return _not_fast_forward(
                        ret,
                        base_rev,
                        remote_rev,
                        branch,
                        local_branch,
                        comments)
                merge_action = 'hard-reset'
            elif fast_forward is True:
                merge_action = 'fast-forwarded'
            else:
                merge_action = 'updated'

            if base_branch is None:
                # No local branch, no upstream tracking branch
                upstream = None
            else:
                try:
                    upstream = __salt__['git.rev_parse'](
                        target,
                        base_branch + '@{upstream}',
                        opts=['--abbrev-ref'],
                        user=user,
                        ignore_retcode=True)
                except CommandExecutionError:
                    # There is a local branch but the rev-parse command
                    # failed, so that means there is no upstream tracking
                    # branch. This could be because it is just not set, or
                    # because the branch was checked out to a SHA1 or tag
                    # instead of a branch. Set upstream to False to make a
                    # distinction between the case above where there is no
                    # local_branch (when the local checkout is an empty
                    # repository).
                    upstream = False

            if remote in remotes:
                fetch_url = remotes[remote]['fetch']
            else:
                log.debug(
                    'Remote \'{0}\' not found in git checkout at {1}'
                    .format(remote, target)
                )
                fetch_url = None

            if remote_rev is not None and desired_fetch_url != fetch_url:
                if __opts__['test']:
                    ret['changes']['remotes/{0}'.format(remote)] = {
                        'old': salt.utils.url.redact_http_basic_auth(fetch_url),
                        'new': redacted_fetch_url
                    }
                    actions = [
                        'Remote \'{0}\' would be set to {1}'.format(
                            remote,
                            redacted_fetch_url
                        )
                    ]
                    if not has_remote_rev:
                        actions.append('Remote would be fetched')
                    if not _revs_equal(local_rev,
                                       remote_rev,
                                       remote_rev_type):
                        ret['changes']['revision'] = {
                            'old': local_rev, 'new': remote_rev
                        }
                        if fast_forward is False:
                            ret['changes']['forced update'] = True
                        actions.append(
                            'Repository would be {0} to {1}'.format(
                                merge_action,
                                _short_sha(remote_rev)
                            )
                        )
                    if ret['changes']:
                        return _neutral_test(ret, _format_comments(actions))
                    else:
                        return _uptodate(ret,
                                         target,
                                         _format_comments(actions))

                # The fetch_url for the desired remote does not match the
                # specified URL (or the remote does not exist), so set the
                # remote URL.
                __salt__['git.remote_set'](target,
                                           url=name,
                                           remote=remote,
                                           user=user,
                                           https_user=https_user,
                                           https_pass=https_pass)
                ret['changes']['remotes/{0}'.format(remote)] = {
                    'old': salt.utils.url.redact_http_basic_auth(fetch_url),
                    'new': redacted_fetch_url
                }
                comments.append(
                    'Remote \'{0}\' set to {1}'.format(
                        remote,
                        redacted_fetch_url
                    )
                )

            if remote_rev is not None:
                if __opts__['test']:
                    if not _revs_equal(local_rev, remote_rev, remote_rev_type):
                        ret['changes']['revision'] = {
                            'old': local_rev, 'new': remote_rev
                        }
                    actions = []
                    if not has_remote_rev:
                        actions.append(
                            'Remote \'{0}\' would be fetched'
                            .format(remote)
                        )
                    if branch is not None:
                        if branch != local_branch:
                            ret['changes']['local branch'] = {
                                'old': local_branch, 'new': branch
                            }
                            if branch not in all_local_branches:
                                actions.append(
                                    'New branch \'{0}\' would be checked '
                                    'out, with {1} ({2}) as a starting '
                                    'point'.format(
                                        branch,
                                        desired_upstream
                                            if desired_upstream
                                            else rev,
                                        _short_sha(remote_rev)
                                    )
                                )
                                if desired_upstream:
                                    actions.append(
                                        'Tracking branch would be set to {0}'
                                        .format(desired_upstream)
                                    )
                            else:
                                if fast_forward is False:
                                    ret['changes']['hard reset'] = True
                                actions.append(
                                    'Branch \'{0}\' would be checked out '
                                    'and {1} to {2}'.format(
                                        branch,
                                        merge_action,
                                        _short_sha(remote_rev)
                                    )
                                )
                    else:
                        if not _revs_equal(local_rev,
                                           remote_rev,
                                           remote_rev_type):
                            if fast_forward is True:
                                actions.append(
                                    'Repository would be fast-forwarded from '
                                    '{0} to {1}'.format(
                                        _short_sha(local_rev),
                                        _short_sha(remote_rev)
                                    )
                                )
                            else:
                                actions.append(
                                    'Repository would be {0} from {1} to {2}'
                                    .format(
                                        'hard-reset'
                                            if force_reset and has_remote_rev
                                            else 'updated',
                                        _short_sha(local_rev),
                                        _short_sha(remote_rev)
                                    )
                                )

                    # Check if upstream needs changing
                    upstream_changed = False
                    if not upstream and desired_upstream:
                        upstream_changed = True
                        actions.append(
                            'Tracking branch would be set to {0}'.format(
                                desired_upstream
                            )
                        )
                    elif upstream and desired_upstream is False:
                        upstream_changed = True
                        actions.append(
                            'Tracking branch would be unset'
                        )
                    elif desired_upstream and upstream != desired_upstream:
                        upstream_changed = True
                        actions.append(
                            'Tracking branch would be '
                            'updated to {0}'.format(desired_upstream)
                        )
                    if upstream_changed:
                        ret['changes']['upstream'] = {
                            'old': upstream,
                            'new': desired_upstream
                        }
                    if ret['changes']:
                        return _neutral_test(ret, _format_comments(actions))
                    else:
                        return _uptodate(ret,
                                         target,
                                         _format_comments(actions))

                if not upstream and desired_upstream:
                    upstream_action = (
                        'Tracking branch was set to {0}'.format(
                            desired_upstream
                        )
                    )
                    branch_opts = [set_upstream, desired_upstream]
                elif upstream and desired_upstream is False:
                    upstream_action = 'Tracking branch was unset'
                    branch_opts = ['--unset-upstream']
                elif desired_upstream and upstream != desired_upstream:
                    upstream_action = (
                        'Tracking branch was updated to {0}'.format(
                            desired_upstream
                        )
                    )
                    branch_opts = [set_upstream, desired_upstream]
                else:
                    branch_opts = None

                if not has_remote_rev:
                    try:
                        output = __salt__['git.fetch'](
                            target,
                            remote=remote,
                            force=force_fetch,
                            refspecs=refspecs,
                            user=user,
                            identity=identity)
                    except CommandExecutionError as exc:
                        msg = 'Fetch failed'
                        if isinstance(exc, CommandExecutionError):
                            msg += (
                                '. Set \'force_fetch\' to True to force '
                                'the fetch if the failure was due to it '
                                'bein non-fast-forward. Output of the '
                                'fetch command follows:\n\n'
                            )
                            msg += _strip_exc(exc)
                        else:
                            msg += ':\n\n' + str(exc)
                        return _fail(ret, msg, comments)
                    else:
                        fetch_changes = _parse_fetch(output)
                        if fetch_changes:
                            ret['changes']['fetch'] = fetch_changes

                    try:
                        __salt__['git.rev_parse'](
                            target,
                            remote_rev + '^{commit}',
                            ignore_retcode=True)
                    except CommandExecutionError as exc:
                        return _fail(
                            ret,
                            'Fetch did not successfully retrieve rev '
                            '{0}: {1}'.format(rev, exc)
                        )

                    # Now that we've fetched, check again whether or not
                    # the update is a fast-forward.
                    if base_rev is None:
                        fast_forward = True
                    else:
                        fast_forward = __salt__['git.merge_base'](
                            target,
                            refs=[base_rev, remote_rev],
                            is_ancestor=True,
                            user=user)

                    if fast_forward is False and not force_reset:
                        return _not_fast_forward(
                            ret,
                            base_rev,
                            remote_rev,
                            branch,
                            local_branch,
                            comments)

                if branch is not None and branch != local_branch:
                    local_changes = __salt__['git.status'](target,
                                                           user=user)
                    if local_changes and not force_checkout:
                        return _fail(
                            ret,
                            'Local branch \'{0}\' has uncommitted '
                            'changes. Set \'force_checkout\' to discard '
                            'them and proceed.'
                        )

                    # TODO: Maybe re-retrieve all_local_branches to handle
                    # the corner case where the destination branch was
                    # added to the local checkout during a fetch that takes
                    # a long time to complete.
                    if branch not in all_local_branches:
                        if rev == 'HEAD':
                            checkout_rev = head_ref
                        else:
                            checkout_rev = desired_upstream \
                                if desired_upstream \
                                else rev
                        checkout_opts = ['-b', branch]
                    else:
                        checkout_rev = branch
                        checkout_opts = []
                    __salt__['git.checkout'](target,
                                             checkout_rev,
                                             force=force_checkout,
                                             opts=checkout_opts,
                                             user=user)
                    ret['changes']['local branch'] = {
                        'old': local_branch, 'new': branch
                    }

                if fast_forward is False:
                    if rev == 'HEAD':
                        reset_ref = head_ref
                    else:
                        reset_ref = desired_upstream \
                            if desired_upstream \
                            else rev
                    __salt__['git.reset'](
                        target,
                        opts=['--hard', remote_rev],
                        user=user
                    )
                    ret['changes']['forced update'] = True
                    comments.append(
                        'Repository was hard-reset to {0} ({1})'.format(
                            reset_ref,
                            _short_sha(remote_rev)
                        )
                    )

                if branch_opts is not None:
                    __salt__['git.branch'](
                        target,
                        base_branch,
                        opts=branch_opts,
                        user=user)
                    ret['changes']['upstream'] = {
                        'old': upstream,
                        'new': desired_upstream if desired_upstream
                            else None
                    }
                    comments.append(upstream_action)

                # Fast-forward to the desired revision
                if fast_forward is True \
                        and not _revs_equal(base_rev,
                                            remote_rev,
                                            remote_rev_type):
                    if desired_upstream:
                        # Check first to see if we are on a branch before
                        # trying to merge changes. (The call to
                        # git.symbolic_ref will only return output if HEAD
                        # points to a branch.)
                        if __salt__['git.symbolic_ref'](target,
                                                        'HEAD',
                                                        opts=['--quiet'],
                                                        ignore_retcode=True):
                            merge_rev = head_ref \
                                if rev == 'HEAD' \
                                else desired_upstream
                            __salt__['git.merge'](
                                target,
                                rev=merge_rev,
                                opts=['--ff-only'],
                                user=user
                            )
                            comments.append(
                                'Repository was fast-forwarded to {0} ({1})'
                                .format(merge_rev, _short_sha(remote_rev))
                            )
                        else:
                            # Shouldn't ever happen but fail with a meaningful
                            # error message if it does.
                            msg = (
                                'Unable to merge {0}, HEAD is detached'
                                .format(desired_upstream)
                            )
                    else:
                        # Update is a fast forward, but we cannot merge to that
                        # commit so we'll reset to it.
                        __salt__['git.reset'](
                            target,
                            opts=['--hard',
                                  remote_rev if rev == 'HEAD' else rev],
                            user=user
                        )
                        comments.append(
                            'Repository was reset to {0} (fast-forward)'
                            .format(rev)
                        )

                # TODO: Figure out how to add submodule update info to
                # test=True return data, and changes dict.
                if submodules:
                    __salt__['git.submodule'](target,
                                              'update',
                                              opts=['--recursive', '--init'],
                                              user=user,
                                              identity=identity)
            elif bare:
                if __opts__['test']:
                    msg = (
                        'Bare repository at {0} would be fetched'
                        .format(target)
                    )
                    if ret['changes']:
                        return _neutral_test(ret, msg)
                    else:
                        return _uptodate(ret, target, msg)
                output = __salt__['git.fetch'](
                    target,
                    remote=remote,
                    force=force_fetch,
                    refspecs=refspecs,
                    user=user,
                    identity=identity)
                fetch_changes = _parse_fetch(output)
                if fetch_changes:
                    ret['changes']['fetch'] = fetch_changes
                comments.append(
                    'Bare repository at {0} was fetched'.format(target)
                )

            try:
                new_rev = __salt__['git.revision'](
                    cwd=target,
                    user=user,
                    ignore_retcode=True)
            except CommandExecutionError:
                new_rev = None

        except Exception as exc:
            log.error(
                'Unexpected exception in git.latest state',
                exc_info=True
            )
            if isinstance(exc, CommandExecutionError):
                msg = _strip_exc(exc)
            else:
                msg = str(exc)
            return _fail(ret, msg, comments)

        if not bare and not _revs_equal(new_rev,
                                        remote_rev,
                                        remote_rev_type):
            return _fail(ret, 'Failed to update repository', comments)

        if local_rev != new_rev:
            log.info(
                'Repository {0} updated: {1} => {2}'.format(
                    target, local_rev, new_rev)
            )
            ret['comment'] = _format_comments(comments)
            ret['changes']['revision'] = {'old': local_rev, 'new': new_rev}
        else:
            return _uptodate(ret, target, comments)
    else:
        if os.path.isdir(target):
            if force_clone:
                # Clone is required, and target directory exists, but the
                # ``force`` option is enabled, so we need to clear out its
                # contents to proceed.
                if __opts__['test']:
                    ret['changes']['forced clone'] = True
                    ret['changes']['new'] = name + ' => ' + target
                    return _neutral_test(
                        ret,
                        'Target directory {0} exists. Since force_clone=True, '
                        'the contents of {0} would be deleted, and {1} would '
                        'be cloned into this directory.'.format(target, name)
                    )
                log.debug(
                    'Removing contents of {0} to clone repository {1} in its '
                    'place (force_clone=True set in git.latest state)'
                    .format(target, name)
                )
                try:
                    if os.path.islink(target):
                        os.unlink(target)
                    else:
                        salt.utils.rm_rf(target)
                except OSError as exc:
                    return _fail(
                        ret,
                        'Unable to remove {0}: {1}'.format(target, exc),
                        comments
                    )
                else:
                    ret['changes']['forced clone'] = True
            # Clone is required, but target dir exists and is non-empty. We
            # can't proceed.
            elif os.listdir(target):
                return _fail(
                    ret,
                    'Target \'{0}\' exists, is non-empty and is not a git '
                    'repository. Set the \'force_clone\' option to True to '
                    'remove this directory\'s contents and proceed with '
                    'cloning the remote repository'.format(target)
                )

        log.debug(
            'Target {0} is not found, \'git clone\' is required'.format(target)
        )
        if __opts__['test']:
            ret['changes']['new'] = name + ' => ' + target
            return _neutral_test(
                ret,
                'Repository {0} would be cloned to {1}'.format(
                    name, target
                )
            )
        try:
            clone_opts = ['--mirror'] if mirror else ['--bare'] if bare else []
            if remote != 'origin':
                clone_opts.extend(['--origin', remote])
            if depth is not None:
                clone_opts.extend(['--depth', str(depth)])

            # We're cloning a fresh repo, there is no local branch or revision
            local_branch = local_rev = None

            __salt__['git.clone'](target,
                                  name,
                                  user=user,
                                  opts=clone_opts,
                                  identity=identity,
                                  https_user=https_user,
                                  https_pass=https_pass)
            ret['changes']['new'] = name + ' => ' + target
            comments.append(
                '{0} cloned to {1}{2}'.format(
                    name,
                    target,
                    ' as mirror' if mirror
                        else ' as bare repository' if bare
                        else ''
                )
            )

            if not bare:
                if not remote_rev:
                    if rev != 'HEAD':
                        # No HEAD means the remote repo is empty, which means
                        # our new clone will also be empty. This state has
                        # failed, since a rev was specified but no matching rev
                        # exists on the remote host.
                        msg = (
                            '{{0}} was cloned but is empty, so {0}/{1} '
                            'cannot be checked out'.format(remote, rev)
                        )
                        log.error(msg.format(name))
                        return _fail(ret, msg.format('Repository'), comments)
                else:
                    if remote_rev_type == 'tag' \
                            and rev not in __salt__['git.list_tags'](
                                target, user=user):
                        return _fail(
                            ret,
                            'Revision \'{0}\' does not exist in clone'
                            .format(rev),
                            comments
                        )

                    if branch is not None:
                        if branch not in \
                                __salt__['git.list_branches'](target,
                                                              user=user):
                            if rev == 'HEAD':
                                checkout_rev = head_ref
                            else:
                                checkout_rev = desired_upstream \
                                    if desired_upstream \
                                    else rev
                            __salt__['git.checkout'](target,
                                                     checkout_rev,
                                                     opts=['-b', branch],
                                                     user=user)
                            comments.append(
                                'Branch \'{0}\' checked out, with {1} ({2}) '
                                'as a starting point'.format(
                                    branch,
                                    desired_upstream
                                        if desired_upstream
                                        else rev,
                                    _short_sha(remote_rev)
                                )
                            )

                    local_rev, local_branch = \
                        _get_local_rev_and_branch(target, user)

                    if not _revs_equal(local_rev, remote_rev, remote_rev_type):
                        if rev == 'HEAD':
                            # Shouldn't happen, if we just cloned the repo and
                            # than the remote HEAD and remote_rev should be the
                            # same SHA1.
                            reset_ref = head_ref
                        else:
                            reset_ref = desired_upstream \
                                if desired_upstream \
                                else rev
                        __salt__['git.reset'](
                            target,
                            opts=['--hard', remote_rev],
                            user=user
                        )
                        comments.append(
                            'Repository was reset to {0} ({1})'.format(
                                reset_ref,
                                _short_sha(remote_rev)
                            )
                        )

                    try:
                        upstream = __salt__['git.rev_parse'](
                            target,
                            local_branch + '@{upstream}',
                            opts=['--abbrev-ref'],
                            user=user,
                            ignore_retcode=True)
                    except CommandExecutionError:
                        upstream = False

                    if not upstream and desired_upstream:
                        upstream_action = (
                            'Tracking branch was set to {0}'.format(
                                desired_upstream
                            )
                        )
                        branch_opts = [set_upstream, desired_upstream]
                    elif upstream and desired_upstream is False:
                        upstream_action = 'Tracking branch was unset'
                        branch_opts = ['--unset-upstream']
                    elif desired_upstream and upstream != desired_upstream:
                        upstream_action = (
                            'Tracking branch was updated to {0}'.format(
                                desired_upstream
                            )
                        )
                        branch_opts = [set_upstream, desired_upstream]
                    else:
                        branch_opts = None

                    if branch_opts is not None:
                        __salt__['git.branch'](
                            target,
                            local_branch,
                            opts=branch_opts,
                            user=user)
                        comments.append(upstream_action)

            if submodules and remote_rev:
                __salt__['git.submodule'](target,
                                          'update',
                                          opts=['--recursive', '--init'],
                                          user=user,
                                          identity=identity)

            try:
                new_rev = __salt__['git.revision'](
                    cwd=target,
                    user=user,
                    ignore_retcode=True)
            except CommandExecutionError:
                new_rev = None

        except Exception as exc:
            log.error(
                'Unexpected exception in git.latest state',
                exc_info=True
            )
            if isinstance(exc, CommandExecutionError):
                msg = _strip_exc(exc)
            else:
                msg = str(exc)
            return _fail(ret, msg, comments)

        msg = _format_comments(comments)
        log.info(msg)
        ret['comment'] = msg
        if new_rev is not None:
            ret['changes']['revision'] = {'old': None, 'new': new_rev}
    return ret


def present(name,
            force=False,
            bare=True,
            template=None,
            separate_git_dir=None,
            shared=None,
            user=None):
    '''
    Ensure that a repository exists in the given directory

    .. warning::
        If the minion has Git 2.5 or later installed, ``name`` points to a
        worktree_, and ``force`` is set to ``True``, then the worktree will be
        deleted. This has been corrected in Salt 2015.8.0.

    name
        Path to the directory

        .. versionchanged:: 2015.8.0
            This path must now be absolute

    force : False
        If ``True``, and if ``name`` points to an existing directory which does
        not contain a git repository, then the contents of that directory will
        be recursively removed and a new repository will be initialized in its
        place.

    bare : True
        If ``True``, and a repository must be initialized, then the repository
        will be a bare repository.

        .. note::
            This differs from the default behavior of :py:func:`git.init
            <salt.modules.git.init>`, make sure to set this value to ``False``
            if a bare repo is not desired.

    template
        If a new repository is initialized, this argument will specify an
        alternate `template directory`_

        .. versionadded:: 2015.8.0

    separate_git_dir
        If a new repository is initialized, this argument will specify an
        alternate ``$GIT_DIR``

        .. versionadded:: 2015.8.0

    shared
        Set sharing permissions on git repo. See `git-init(1)`_ for more
        details.

        .. versionadded:: 2015.5.0

    user
        User under which to run git commands. By default, commands are run by
        the user under which the minion is running.

        .. versionadded:: 0.17.0

    .. _`git-init(1)`: http://git-scm.com/docs/git-init
    .. _`worktree`: http://git-scm.com/docs/git-worktree
    '''
    ret = {'name': name, 'result': True, 'comment': '', 'changes': {}}

    # If the named directory is a git repo return True
    if os.path.isdir(name):
        if bare and os.path.isfile(os.path.join(name, 'HEAD')):
            return ret
        elif not bare and \
                (os.path.isdir(os.path.join(name, '.git')) or
                 __salt__['git.is_worktree'](name)):
            return ret
        # Directory exists and is not a git repo, if force is set destroy the
        # directory and recreate, otherwise throw an error
        elif force:
            # Directory exists, and the ``force`` option is enabled, so we need
            # to clear out its contents to proceed.
            if __opts__['test']:
                ret['changes']['new'] = name
                ret['changes']['forced init'] = True
                return _neutral_test(
                    ret,
                    'Target directory {0} exists. Since force=True, the '
                    'contents of {0} would be deleted, and a {1}repository '
                    'would be initialized in its place.'
                    .format(name, 'bare ' if bare else '')
                )
            log.debug(
                'Removing contents of {0} to initialize {1}repository in its '
                'place (force=True set in git.present state)'
                .format(name, 'bare ' if bare else '')
            )
            try:
                if os.path.islink(name):
                    os.unlink(name)
                else:
                    salt.utils.rm_rf(name)
            except OSError as exc:
                return _fail(
                    ret,
                    'Unable to remove {0}: {1}'.format(name, exc)
                )
            else:
                ret['changes']['forced init'] = True
        elif os.listdir(name):
            return _fail(
                ret,
                'Target \'{0}\' exists, is non-empty, and is not a git '
                'repository. Set the \'force\' option to True to remove '
                'this directory\'s contents and proceed with initializing a '
                'repository'.format(name)
            )

    # Run test is set
    if __opts__['test']:
        ret['changes']['new'] = name
        return _neutral_test(
            ret,
            'New {0}repository would be created'.format(
                'bare ' if bare else ''
            )
        )

    __salt__['git.init'](cwd=name,
                         bare=bare,
                         template=template,
                         separate_git_dir=separate_git_dir,
                         shared=shared,
                         user=user)

    actions = [
        'Initialized {0}repository in {1}'.format(
            'bare ' if bare else '',
            name
        )
    ]
    if template:
        actions.append('Template directory set to {0}'.format(template))
    if separate_git_dir:
        actions.append('Gitdir set to {0}'.format(separate_git_dir))
    message = '. '.join(actions)
    if len(actions) > 1:
        message += '.'
    log.info(message)
    ret['changes']['new'] = name
    ret['comment'] = message
    return ret


def config_unset(name,
                 value_regex=None,
                 repo=None,
                 user=None,
                 **kwargs):
    r'''
    .. versionadded:: 2015.8.0

    Ensure that the named config key is not present

    name
        The name of the configuration key to unset. This value can be a regex,
        but the regex must match the entire key name. For example, ``foo\.``
        would not match all keys in the ``foo`` section, it would be necessary
        to use ``foo\..+`` to do so.

    value_regex
        Regex indicating the values to unset for the matching key(s)

        .. note::
            This option behaves differently depending on whether or not ``all``
            is set to ``True``. If it is, then all values matching the regex
            will be deleted (this is the only way to delete mutliple values
            from a multivar). If ``all`` is set to ``False``, then this state
            will fail if the regex matches more than one value in a multivar.

    all : False
        If ``True``, unset all matches

    repo
        Location of the git repository for which the config value should be
        set. Required unless ``global`` is set to ``True``.

    user
        Optional name of a user as whom `git config` will be run

    global : False
        If ``True``, this will set a global git config option


    **Examples:**

    .. code-block:: yaml

        # Value matching 'baz'
        mylocalrepo:
          git.config_unset:
            - name: foo.bar
            - value_regex: 'baz'
            - repo: /path/to/repo

        # Ensure entire multivar is unset
        mylocalrepo:
          git.config_unset:
            - name: foo.bar
            - all: True

        # Ensure all variables in 'foo' section are unset, including multivars
        mylocalrepo:
          git.config_unset:
            - name: 'foo\..+'
            - all: True

        # Ensure that global config value is unset
        mylocalrepo:
          git.config_unset:
            - name: foo.bar
            - global: True
    '''
    ret = {'name': name,
           'changes': {},
           'result': True,
           'comment': 'No matching keys are set'}

    # Sanitize kwargs and make sure that no invalid ones were passed. This
    # allows us to accept 'global' as an argument to this function without
    # shadowing global(), while also not allowing unwanted arguments to be
    # passed.
    kwargs = salt.utils.clean_kwargs(**kwargs)
    global_ = kwargs.pop('global', False)
    all_ = kwargs.pop('all', False)
    if kwargs:
        return _fail(
            ret,
            salt.utils.invalid_kwargs(kwargs, raise_exc=False)
        )

    if not global_ and not repo:
        return _fail(
            ret,
            'Non-global config options require the \'repo\' argument to be '
            'set'
        )

    if not isinstance(name, six.string_types):
        name = str(name)
    if value_regex is not None:
        if not isinstance(value_regex, six.string_types):
            value_regex = str(value_regex)

    # Ensure that the key regex matches the full key name
    key = '^' + name.lstrip('^').rstrip('$') + '$'

    # Get matching keys/values
    pre_matches = __salt__['git.config_get_regexp'](
        cwd=repo,
        key=key,
        value_regex=value_regex,
        user=user,
        ignore_retcode=True,
        **{'global': global_}
    )

    if not pre_matches:
        # No changes need to be made
        return ret

    # Perform sanity check on the matches. We can't proceed if the value_regex
    # matches more than one value in a given key, and 'all' is not set to True
    if not all_:
        greedy_matches = ['{0} ({1})'.format(x, ', '.join(y))
                          for x, y in six.iteritems(pre_matches)
                          if len(y) > 1]
        if greedy_matches:
            if value_regex is not None:
                return _fail(
                    ret,
                    'Multiple values are matched by value_regex for the '
                    'following keys (set \'all\' to True to force removal): '
                    '{0}'.format('; '.join(greedy_matches))
                )
            else:
                return _fail(
                    ret,
                    'Multivar(s) matched by the key expression (set \'all\' '
                    'to True to force removal): {0}'.format(
                        '; '.join(greedy_matches)
                    )
                )

    if __opts__['test']:
        ret['changes'] = pre_matches
        return _neutral_test(
            ret,
            '{0} key(s) would have value(s) unset'.format(len(pre_matches))
        )

    if value_regex is None:
        pre = pre_matches
    else:
        # Get all keys matching the key expression, so we can accurately report
        # on changes made.
        pre = __salt__['git.config_get_regexp'](
            cwd=repo,
            key=key,
            value_regex=None,
            user=user,
            ignore_retcode=True,
            **{'global': global_}
        )

    failed = []
    # Unset the specified value(s). There is no unset for regexes so loop
    # through the pre_matches dict and unset each matching key individually.
    for key_name in pre_matches:
        try:
            __salt__['git.config_unset'](
                cwd=repo,
                key=name,
                value_regex=value_regex,
                all=all_,
                user=user,
                **{'global': global_}
            )
        except CommandExecutionError as exc:
            msg = 'Failed to unset \'{0}\''.format(key_name)
            if value_regex is not None:
                msg += ' using value_regex \'{1}\''
            msg += ': ' + _strip_exc(exc)
            log.error(msg)
            failed.append(key_name)

    if failed:
        return _fail(
            ret,
            'Error(s) occurred unsetting values for the following keys (see '
            'the minion log for details): {0}'.format(', '.join(failed))
        )

    post = __salt__['git.config_get_regexp'](
        cwd=repo,
        key=key,
        value_regex=None,
        user=user,
        ignore_retcode=True,
        **{'global': global_}
    )

    for key_name, values in six.iteritems(pre):
        if key_name not in post:
            ret['changes'][key_name] = pre[key_name]
        unset = [x for x in pre[key_name] if x not in post[key_name]]
        if unset:
            ret['changes'][key_name] = unset

    if value_regex is None:
        post_matches = post
    else:
        post_matches = __salt__['git.config_get_regexp'](
            cwd=repo,
            key=key,
            value_regex=value_regex,
            user=user,
            ignore_retcode=True,
            **{'global': global_}
        )

    if post_matches:
        failed = ['{0} ({1})'.format(x, ', '.join(y))
                  for x, y in six.iteritems(post_matches)]
        return _fail(
            ret,
            'Failed to unset value(s): {0}'.format('; '.join(failed))
        )

    ret['comment'] = 'Value(s) successfully unset'
    return ret


def config_set(name,
               cwd=None,
               value=None,
               multivar=None,
               repo=None,
               user=None,
               **kwargs):
    '''
    .. versionadded:: 2014.7.0
    .. versionchanged:: 2015.8.0
        Renamed from ``git.config`` to ``git.config_set``. For earlier
        versions, use ``git.config``.

    Ensure that a config value is set to the desired value(s)

    name
        Name of the git config value to set

    value
        Set a single value for the config item

    multivar
        Set multiple values for the config item

        .. note::
            The order matters here, if the same parameters are set but in a
            different order, they will be removed and replaced in the order
            specified.

        .. versionadded:: 2015.8.0

    repo
        Location of the git repository for which the config value should be
        set. Required unless ``global`` is set to ``True``.

    user
        Optional name of a user as whom `git config` will be run

    global : False
        If ``True``, this will set a global git config option

        .. versionchanged:: 2015.8.0
            Option renamed from ``is_global`` to ``global``. For earlier
            versions, use ``is_global``.


    **Local Config Example:**

    .. code-block:: yaml

        # Single value
        mylocalrepo:
          git.config_set:
            - name: user.email
            - value: foo@bar.net
            - repo: /path/to/repo

        # Multiple values
        mylocalrepo:
          git.config_set:
            - name: mysection.myattribute
            - multivar:
              - foo
              - bar
              - baz
            - repo: /path/to/repo

    **Global Config Example (User ``foo``):**

    .. code-block:: yaml

        mylocalrepo:
          git.config_set:
            - name: user.name
            - value: Foo Bar
            - user: foo
            - global: True
    '''
    ret = {'name': name,
           'changes': {},
           'result': True,
           'comment': ''}

    if value is not None and multivar is not None:
        return _fail(
            ret,
            'Only one of \'value\' and \'multivar\' is permitted'
        )

    # Sanitize kwargs and make sure that no invalid ones were passed. This
    # allows us to accept 'global' as an argument to this function without
    # shadowing global(), while also not allowing unwanted arguments to be
    # passed.
    kwargs = salt.utils.clean_kwargs(**kwargs)
    global_ = kwargs.pop('global', False)
    is_global = kwargs.pop('is_global', False)
    if kwargs:
        return _fail(
            ret,
            salt.utils.invalid_kwargs(kwargs, raise_exc=False)
        )

    if is_global:
        salt.utils.warn_until(
            'Nitrogen',
            'The \'is_global\' argument to the git.config_set state has been '
            'deprecated, please use \'global\' instead.'
        )
        global_ = is_global

    if not global_ and not repo:
        return _fail(
            ret,
            'Non-global config options require the \'repo\' argument to be '
            'set'
        )

    if not isinstance(name, six.string_types):
        name = str(name)
    if value is not None:
        if not isinstance(value, six.string_types):
            value = str(value)
        value_comment = '\'' + value + '\''
        desired = [value]
    if multivar is not None:
        if not isinstance(multivar, list):
            try:
                multivar = multivar.split(',')
            except AttributeError:
                multivar = str(multivar).split(',')
        else:
            new_multivar = []
            for item in multivar:
                if isinstance(item, six.string_types):
                    new_multivar.append(item)
                else:
                    new_multivar.append(str(item))
            multivar = new_multivar
        value_comment = multivar
        desired = multivar

    # Get current value
    pre = __salt__['git.config_get'](
        cwd=repo,
        key=name,
        user=user,
        ignore_retcode=True,
        **{'all': True, 'global': global_}
    )

    if desired == pre:
        ret['comment'] = '{0}\'{1}\' is already set to {2}'.format(
            'Global key ' if global_ else '',
            name,
            value_comment
        )
        return ret

    if __opts__['test']:
        ret['changes'] = {'old': pre, 'new': desired}
        msg = '{0}\'{1}\' would be {2} {3}'.format(
            'Global key ' if global_ else '',
            name,
            'added as' if pre is None else 'set to',
            value_comment
        )
        return _neutral_test(ret, msg)

    try:
        # Set/update config value
        post = __salt__['git.config_set'](
            cwd=repo,
            key=name,
            value=value,
            multivar=multivar,
            user=user,
            **{'global': global_}
        )
    except CommandExecutionError as exc:
        return _fail(
            ret,
            'Failed to set {0}\'{1}\' to {2}: {3}'.format(
                'global key ' if global_ else '',
                name,
                value_comment,
                _strip_exc(exc)
            )
        )

    if pre != post:
        ret['changes'][name] = {'old': pre, 'new': post}

    if post != desired:
        return _fail(
            ret,
            'Failed to set {0}\'{1}\' to {2}'.format(
                'global key ' if global_ else '',
                name,
                value_comment
            )
        )

    ret['comment'] = '{0}\'{1}\' was {2} {3}'.format(
        'Global key ' if global_ else '',
        name,
        'added as' if pre is None else 'set to',
        value_comment
    )
    return ret


def config(name, value=None, multivar=None, repo=None, user=None, **kwargs):
    '''
    Pass through to git.config_set and display a deprecation warning
    '''
    salt.utils.warn_until(
        'Nitrogen',
        'The \'git.config\' state has been renamed to \'git.config_set\', '
        'please update your SLS files'
    )
    return config_set(name=name,
                      value=value,
                      multivar=multivar,
                      repo=repo,
                      user=user,
                      **kwargs)


def mod_run_check(cmd_kwargs, onlyif, unless):
    '''
    Execute the onlyif and unless logic. Return a result dict if:

    * onlyif failed (onlyif != 0)
    * unless succeeded (unless == 0)

    Otherwise, returns ``True``
    '''
    cmd_kwargs = copy.deepcopy(cmd_kwargs)
    cmd_kwargs['python_shell'] = True
    if onlyif:
        if __salt__['cmd.retcode'](onlyif, **cmd_kwargs) != 0:
            return {'comment': 'onlyif execution failed',
                    'skip_watch': True,
                    'result': True}

    if unless:
        if __salt__['cmd.retcode'](unless, **cmd_kwargs) == 0:
            return {'comment': 'unless execution succeeded',
                    'skip_watch': True,
                    'result': True}

    # No reason to stop, return True
    return True
