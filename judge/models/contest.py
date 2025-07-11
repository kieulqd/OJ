import hashlib
import hmac
from datetime import date, timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models, transaction
from django.db.models import CASCADE, Q
from django.urls import reverse
from django.utils import timezone
from django.utils.functional import cached_property
from django.utils.translation import gettext_lazy as _
from jsonfield import JSONField
from lupa import LuaRuntime
from moss import MOSS_LANG_C, MOSS_LANG_CC, MOSS_LANG_JAVA, MOSS_LANG_PASCAL, MOSS_LANG_PYTHON

from judge import contest_format, event_poster as event
from judge.models.problem import Problem
from judge.models.profile import Organization, Profile
from judge.models.submission import Submission
from judge.ratings import rate_contest
from judge.utils.unicode import utf8bytes

__all__ = ['Contest', 'ContestTag', 'ContestAnnouncement', 'ContestParticipation', 'ContestProblem',
           'ContestSubmission', 'Rating']


class MinValueOrNoneValidator(MinValueValidator):
    def compare(self, a, b):
        return a is not None and b is not None and super().compare(a, b)


class ContestTag(models.Model):
    color_validator = RegexValidator('^#(?:[A-Fa-f0-9]{3}){1,2}$', _('Invalid colour.'))

    name = models.CharField(max_length=20, verbose_name=_('tag name'), unique=True,
                            validators=[RegexValidator(r'^[a-z-]+$', message=_('Lowercase letters and hyphens only.'))])
    color = models.CharField(max_length=7, verbose_name=_('tag colour'), validators=[color_validator])
    description = models.TextField(verbose_name=_('tag description'), blank=True)

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse('contest_tag', args=[self.name])

    @property
    def text_color(self, cache={}):
        if self.color not in cache:
            if len(self.color) == 4:
                r, g, b = [ord(bytes.fromhex(i * 2)) for i in self.color[1:]]
            else:
                r, g, b = [i for i in bytes.fromhex(self.color[1:])]
            cache[self.color] = '#000' if 299 * r + 587 * g + 144 * b > 140000 else '#fff'
        return cache[self.color]

    class Meta:
        verbose_name = _('contest tag')
        verbose_name_plural = _('contest tags')


class Contest(models.Model):
    SCOREBOARD_VISIBLE = 'V'
    SCOREBOARD_HIDDEN = 'H'
    SCOREBOARD_AFTER_CONTEST = 'C'
    SCOREBOARD_AFTER_PARTICIPATION = 'P'
    SCOREBOARD_VISIBILITY = (
        (SCOREBOARD_VISIBLE, _('Visible')),
        (SCOREBOARD_HIDDEN, _('Always hidden')),
        (SCOREBOARD_AFTER_CONTEST, _('Hidden for duration of contest')),
        (SCOREBOARD_AFTER_PARTICIPATION, _('Hidden for duration of participation')),
    )
    key = models.CharField(max_length=32, verbose_name=_('contest id'), unique=True,
                           validators=[RegexValidator('^[a-z0-9_]+$', _('Contest id must be ^[a-z0-9_]+$'))])
    name = models.CharField(max_length=100, verbose_name=_('contest name'), db_index=True)
    authors = models.ManyToManyField(Profile, help_text=_('These users will be able to edit the contest.'),
                                     related_name='authors+')
    curators = models.ManyToManyField(Profile, help_text=_('These users will be able to edit the contest, '
                                                           'but will not be listed as authors.'),
                                      related_name='curators+', blank=True)
    testers = models.ManyToManyField(Profile, help_text=_('These users will be able to view the contest, '
                                                          'but not edit it.'),
                                     blank=True, related_name='testers+')
    description = models.TextField(verbose_name=_('description'), blank=True)
    problems = models.ManyToManyField(Problem, verbose_name=_('problems'), through='ContestProblem')
    start_time = models.DateTimeField(verbose_name=_('start time'), db_index=True)
    end_time = models.DateTimeField(verbose_name=_('end time'), db_index=True)
    registration_start = models.DateTimeField(verbose_name=_('registration start time'),
                                              blank=True, null=True, default=None)
    registration_end = models.DateTimeField(verbose_name=_('registration end time'),
                                            blank=True, null=True, default=None)
    time_limit = models.DurationField(verbose_name=_('time limit'), blank=True, null=True)
    frozen_last_minutes = models.IntegerField(verbose_name=_('frozen last minutes'), default=0,
                                              help_text=_('If set, the scoreboard will be frozen for the last X '
                                                          'minutes. Only available for ICPC and VNOJ format.'))
    is_visible = models.BooleanField(verbose_name=_('publicly visible'), default=False,
                                     help_text=_('Should be set even for organization-private contests, where it '
                                                 'determines whether the contest is visible to members of the '
                                                 'specified organizations.'))
    is_rated = models.BooleanField(verbose_name=_('contest rated'), help_text=_('Whether this contest can be rated.'),
                                   default=False)
    view_contest_scoreboard = models.ManyToManyField(Profile, verbose_name=_('view contest scoreboard'), blank=True,
                                                     related_name='view_contest_scoreboard',
                                                     help_text=_('These users will be able to view the scoreboard.'))
    scoreboard_visibility = models.CharField(verbose_name=_('scoreboard visibility'), default=SCOREBOARD_VISIBLE,
                                             help_text=_('Scoreboard visibility through the duration of the contest'),
                                             max_length=1, choices=SCOREBOARD_VISIBILITY)
    scoreboard_cache_timeout = models.PositiveIntegerField(verbose_name=('scoreboard cache timeout'), default=0,
                                                           help_text=_('How long should the scoreboard be cached. '
                                                                       'Set to 0 to disable caching.'))
    show_submission_list = models.BooleanField(default=False,
                                               help_text=_('Allow contestants to view submission list '
                                                           'of others in contest time'))
    use_clarifications = models.BooleanField(verbose_name=_('no comments'),
                                             help_text=_('Use clarification system instead of comments.'),
                                             default=True)
    push_announcements = models.BooleanField(verbose_name=_('push announcements'),
                                             help_text=_('Notify users when there are new announcements.'),
                                             default=False)
    rating_floor = models.IntegerField(verbose_name=_('rating floor'), null=True, blank=True,
                                       help_text=_('Do not rate users who have a lower rating.'))
    rating_ceiling = models.IntegerField(verbose_name=_('rating ceiling'), null=True, blank=True,
                                         help_text=_('Do not rate users who have a higher rating.'))
    rate_all = models.BooleanField(verbose_name=_('rate all'),
                                   help_text=_('Rate users even if they make no submissions.'),
                                   default=False)
    rate_exclude = models.ManyToManyField(Profile, verbose_name=_('exclude from ratings'), blank=True,
                                          related_name='rate_exclude+')
    rate_disqualified = models.BooleanField(verbose_name=_('rate disqualified'),
                                            help_text=_('Rate users even if they are disqualified.'),
                                            default=True)
    is_private = models.BooleanField(verbose_name=_('private to specific users'), default=False)
    private_contestants = models.ManyToManyField(Profile, blank=True, verbose_name=_('private contestants'),
                                                 help_text=_('If private, only these users may see the contest.'),
                                                 related_name='private_contestants+')
    hide_problem_tags = models.BooleanField(verbose_name=_('hide problem tags'),
                                            help_text=_('Whether problem tags should be hidden by default.'),
                                            default=False)
    hide_problem_authors = models.BooleanField(verbose_name=_('hide problem authors'),
                                               help_text=_('Whether problem authors should be hidden by default.'),
                                               default=False)
    run_pretests_only = models.BooleanField(verbose_name=_('run pretests only'),
                                            help_text=_('Whether judges should grade pretests only, versus all '
                                                        'testcases. Commonly set during a contest, then unset '
                                                        'prior to rejudging user submissions when the contest ends.'),
                                            default=False)
    show_short_display = models.BooleanField(verbose_name=_('show short form settings display'),
                                             help_text=_('Whether to show a section containing contest settings '
                                                         'on the contest page or not.'),
                                             default=False)
    is_organization_private = models.BooleanField(verbose_name=_('private to organizations'), default=False)
    organizations = models.ManyToManyField(Organization, blank=True, verbose_name=_('organizations'),
                                           help_text=_('If private, only these organizations may see the contest'))
    og_image = models.CharField(verbose_name=_('OpenGraph image'), default='', max_length=150, blank=True)
    logo_override_image = models.CharField(verbose_name=_('logo override image'), default='', max_length=150,
                                           blank=True,
                                           help_text=_('This image will replace the default site logo for users '
                                                       'inside the contest.'))
    tags = models.ManyToManyField(ContestTag, verbose_name=_('contest tags'), blank=True, related_name='contests')
    user_count = models.IntegerField(verbose_name=_('the amount of live participants'), default=0)
    virtual_count = models.IntegerField(verbose_name=_('the amount of virtual participants'), default=0)
    summary = models.TextField(blank=True, verbose_name=_('contest summary'),
                               help_text=_('Plain-text, shown in meta description tag, e.g. for social media.'))
    access_code = models.CharField(verbose_name=_('access code'), blank=True, default='', max_length=255,
                                   help_text=_('An optional code to prompt contestants before they are allowed '
                                               'to join the contest. Leave it blank to disable.'))
    banned_users = models.ManyToManyField(Profile, verbose_name=_('personae non gratae'), blank=True,
                                          help_text=_('Bans the selected users from joining this contest.'))
    banned_judges = models.ManyToManyField('judge.Judge', verbose_name=_('Banned judges'), blank=True,
                                           help_text=_('Bans the selected judges from judging this contest.'))
    format_name = models.CharField(verbose_name=_('contest format'), default='default', max_length=32,
                                   choices=contest_format.choices(), help_text=_('The contest format module to use.'))
    format_config = JSONField(verbose_name=_('contest format configuration'), null=True, blank=True,
                              help_text=_('A JSON object to serve as the configuration for the chosen contest format '
                                          'module. Leave empty to use None. Exact format depends on the contest format '
                                          'selected.'))
    problem_label_script = models.TextField(verbose_name=_('contest problem label script'), blank=True,
                                            help_text=_('A custom Lua function to generate problem labels. Requires a '
                                                        'single function with an integer parameter, the zero-indexed '
                                                        'contest problem index, and returns a string, the label.'))
    locked_after = models.DateTimeField(verbose_name=_('contest lock'), null=True, blank=True,
                                        help_text=_('Prevent submissions from this contest '
                                                    'from being rejudged after this date.'))
    points_precision = models.IntegerField(verbose_name=_('precision points'), default=3,
                                           validators=[MinValueValidator(0), MaxValueValidator(10)],
                                           help_text=_('Number of digits to round points to.'))
    csv_ranking = models.TextField(verbose_name=_('official ranking'), blank=True,
                                   help_text=_('Official ranking exported from CMS in CSV format.'))
    data_last_downloaded = models.DateTimeField(verbose_name=_('last data download time'), null=True, blank=True)
    disallow_virtual = models.BooleanField(verbose_name=_('Disallow virtual joining'),
                                           help_text=_('Disallow virtual joining after contest has ended.'),
                                           default=False)

    ranking_access_code = models.CharField(verbose_name=_('ranking access code'),
                                           help_text=_('An optional code to view the contest ranking. '
                                                       'Leave it blank to disable.'),
                                           blank=True, default='', max_length=255)

    @cached_property
    def format_class(self):
        return contest_format.formats[self.format_name]

    @cached_property
    def format(self):
        return self.format_class(self, self.format_config)

    @cached_property
    def get_label_for_problem(self):
        if not self.problem_label_script:
            return self.format.get_label_for_problem

        def DENY_ALL(obj, attr_name, is_setting):
            raise AttributeError()
        lua = LuaRuntime(attribute_filter=DENY_ALL, register_eval=False, register_builtins=False)
        return lua.eval(self.problem_label_script)

    def clean(self):
        # Django will complain if you didn't fill in start_time or end_time, so we don't have to.
        if self.start_time and self.end_time and self.start_time >= self.end_time:
            raise ValidationError('What is this? A contest that ended before it starts?')

        if self.registration_start and self.registration_end and self.registration_start >= self.registration_end:
            raise ValidationError('Registration window must start before it ends.')

        if self.registration_start and self.start_time and self.registration_start >= self.start_time:
            raise ValidationError('Registration window must start before the contest starts.')

        if self.registration_end and self.end_time and self.registration_end >= self.end_time:
            raise ValidationError('Registration window must end before the contest ends.')

        self.format_class.validate(self.format_config)

        try:
            # a contest should have at least one problem, with contest problem index 0
            # so test it to see if the script returns a valid label.
            label = self.get_label_for_problem(0)
        except Exception as e:
            raise ValidationError('Contest problem label script: %s' % e)
        else:
            if not isinstance(label, str):
                raise ValidationError('Contest problem label script: script should return a string.')

    def is_in_contest(self, user):
        if user.is_authenticated:
            profile = user.profile
            return profile and profile.current_contest is not None and profile.current_contest.contest == self \
                and (profile.current_contest.contest.can_join or profile.current_contest.spectate)
        return False

    def can_see_own_scoreboard(self, user):
        if self.can_see_full_scoreboard(user):
            return True
        if not self.can_join:
            return False
        if not self.show_scoreboard and not self.is_in_contest(user) and not self.has_completed_contest(user):
            return False
        return True

    def can_see_full_scoreboard(self, user):
        if self.show_scoreboard:
            return True
        if not user.is_authenticated:
            return False
        if user.has_perm('judge.see_private_contest') or user.has_perm('judge.edit_all_contest'):
            return True
        if user.profile.id in self.editor_ids:
            return True
        if self.view_contest_scoreboard.filter(id=user.profile.id).exists():
            return True
        if self.scoreboard_visibility == self.SCOREBOARD_AFTER_PARTICIPATION and self.has_completed_contest(user):
            return True
        return False

    def can_see_full_submission_list(self, user):
        """Logic:
            - If scoreboard is not visible -> False
            - If user can edit the contest -> True
            - If contest is frozen -> False
            - If contest is ended -> True
            - If show_submission_list -> True
            - Otherwise -> False
        """
        # If the user cannot view the full scoreboard, then they should not be
        # able to view the submission list of others.
        if not self.can_see_full_scoreboard(user):
            return False
        if user.is_authenticated and user.profile.id in self.editor_ids:
            return True
        if user.has_perm('judge.edit_all_contest'):
            return True
        # Because we have not yet implemented the freezing logic for the submission list
        # so we should disable the submission list if the contest is frozen
        if self.is_frozen:
            return False
        if self.ended:
            return True
        if self.show_submission_list:
            return True
        return False

    def has_completed_contest(self, user):
        if user.is_authenticated:
            participation = self.users.filter(virtual=ContestParticipation.LIVE, user=user.profile).first()
            if participation and participation.ended:
                return True
        return False

    @cached_property
    def show_scoreboard(self):
        if self.scoreboard_visibility == self.SCOREBOARD_HIDDEN:
            return False
        if not self.can_join:
            return False
        if (self.scoreboard_visibility in (self.SCOREBOARD_AFTER_CONTEST, self.SCOREBOARD_AFTER_PARTICIPATION) and
                not self.ended):
            return False
        return True

    @property
    def contest_window_length(self):
        return self.end_time - self.start_time

    @cached_property
    def _now(self):
        # This ensures that all methods talk about the same now.
        return timezone.now()

    @cached_property
    def require_registration(self):
        return self.registration_start is not None or self.registration_end is not None

    @cached_property
    def can_register(self):
        if not self.require_registration:
            return False
        if self.registration_start and self._now < self.registration_start:
            return False
        if self.registration_end and self._now > self.registration_end:
            return False
        return True

    @cached_property
    def can_join(self):
        return self.start_time <= self._now

    @cached_property
    def frozen_time(self):
        # Don't need to check self.frozen_last_minutes != 0
        return self.end_time - timedelta(minutes=self.frozen_last_minutes)

    @property
    def time_before_register(self):
        if self.registration_start and self._now <= self.registration_start:
            return self.registration_start - self._now
        else:
            return None

    @property
    def time_before_start(self):
        if self.start_time >= self._now:
            return self.start_time - self._now
        else:
            return None

    @property
    def time_before_end(self):
        if self.end_time >= self._now:
            return self.end_time - self._now
        else:
            return None

    @cached_property
    def ended(self):
        return self.end_time < self._now

    @cached_property
    def author_ids(self):
        return Contest.authors.through.objects.filter(contest=self).values_list('profile_id', flat=True)

    @cached_property
    def editor_ids(self):
        return self.author_ids.union(
            Contest.curators.through.objects.filter(contest=self).values_list('profile_id', flat=True))

    @cached_property
    def tester_ids(self):
        return Contest.testers.through.objects.filter(contest=self).values_list('profile_id', flat=True)

    @classmethod
    def get_id_secret(cls, contest_id):
        return (hmac.new(utf8bytes(settings.EVENT_DAEMON_CONTEST_KEY), b'%d' % contest_id, hashlib.sha512)
                    .hexdigest()[:16] + '%08x' % contest_id)

    @cached_property
    def id_secret(self):
        return self.get_id_secret(self.id)

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse('contest_view', args=(self.key,))

    def update_user_count(self):
        self.user_count = self.users.filter(virtual=0).count()
        self.virtual_count = self.users.filter(virtual__gt=0).count()
        self.save()

    update_user_count.alters_data = True

    @property
    def is_frozen(self):
        if self.frozen_last_minutes == 0:
            return False
        if self.format.name == contest_format.ICPCContestFormat.name or \
           self.format.name == contest_format.VNOJContestFormat.name:
            # Keep frozen even if the contest is ended
            return self._now >= self.frozen_time
        return False

    class Inaccessible(Exception):
        pass

    class PrivateContest(Exception):
        pass

    def access_check(self, user):
        # Do unauthenticated check here so we can skip authentication checks later on.
        if not user.is_authenticated:
            # Unauthenticated users can only see visible, non-private contests
            if not self.is_visible:
                raise self.Inaccessible()
            if self.is_private or self.is_organization_private:
                raise self.PrivateContest()
            return

        # If the user can view or edit all contests
        if user.has_perm('judge.see_private_contest') or user.has_perm('judge.edit_all_contest'):
            return

        # User is organizer or curator for contest
        if user.profile.id in self.editor_ids:
            return

        # User is tester for contest
        if user.profile.id in self.tester_ids:
            return

        # Contest is not publicly visible
        if not self.is_visible:
            raise self.Inaccessible()

        # Contest is not private
        if not self.is_private and not self.is_organization_private:
            return

        if self.view_contest_scoreboard.filter(id=user.profile.id).exists():
            return

        in_org = self.organizations.filter(id__in=user.profile.organizations.all()).exists()
        in_users = self.private_contestants.filter(id=user.profile.id).exists()

        if not self.is_private and self.is_organization_private:
            if in_org:
                return
            raise self.PrivateContest()

        if self.is_private and not self.is_organization_private:
            if in_users:
                return
            raise self.PrivateContest()

        if self.is_private and self.is_organization_private:
            if in_org and in_users:
                return
            raise self.PrivateContest()

    def is_accessible_by(self, user):
        try:
            self.access_check(user)
        except (self.Inaccessible, self.PrivateContest):
            return False
        else:
            return True

    def is_editable_by(self, user):
        # If the user can edit all contests
        if user.has_perm('judge.edit_all_contest'):
            return True

        # If the user is a contest organizer or curator
        if user.has_perm('judge.edit_own_contest') and user.profile.id in self.editor_ids:
            return True

        return False

    @classmethod
    def get_public_contests(cls):
        return cls.objects.filter(is_visible=True, is_organization_private=False, is_private=False) \
                          .defer('description').distinct()

    @classmethod
    def get_visible_contests(cls, user):
        if not user.is_authenticated:
            return cls.get_public_contests()

        queryset = cls.objects.defer('description')
        if not (user.has_perm('judge.see_private_contest') or user.has_perm('judge.edit_all_contest')):
            q = Q(is_visible=True)
            q &= (
                Q(view_contest_scoreboard=user.profile) |
                Q(is_organization_private=False, is_private=False) |
                Q(is_organization_private=False, is_private=True, private_contestants=user.profile) |
                Q(is_organization_private=True, is_private=False, organizations__in=user.profile.organizations.all()) |
                Q(is_organization_private=True, is_private=True, organizations__in=user.profile.organizations.all(),
                  private_contestants=user.profile)
            )

            q |= Q(authors=user.profile)
            q |= Q(curators=user.profile)
            q |= Q(testers=user.profile)
            queryset = queryset.filter(q)
        return queryset.distinct()

    def rate(self):
        with transaction.atomic():
            Rating.objects.filter(contest__end_time__range=(self.end_time, self._now)).delete()
            for contest in Contest.objects.filter(
                is_rated=True, end_time__range=(self.end_time, self._now),
            ).order_by('end_time'):
                rate_contest(contest)

    class Meta:
        permissions = (
            ('see_private_contest', _('See private contests')),
            ('edit_own_contest', _('Edit own contests')),
            ('edit_all_contest', _('Edit all contests')),
            ('clone_contest', _('Clone contest')),
            ('moss_contest', _('MOSS contest')),
            ('contest_rating', _('Rate contests')),
            ('contest_access_code', _('Contest access codes')),
            ('create_private_contest', _('Create private contests')),
            ('change_contest_visibility', _('Change contest visibility')),
            ('contest_problem_label', _('Edit contest problem label script')),
            ('lock_contest', _('Change lock status of contest')),
        )
        verbose_name = _('contest')
        verbose_name_plural = _('contests')


class ContestAnnouncement(models.Model):
    contest = models.ForeignKey(Contest, verbose_name=_('announced contest'), on_delete=CASCADE)
    title = models.CharField(max_length=100, verbose_name=_('announcement title'))
    description = models.TextField(verbose_name=_('announcement body'))
    date = models.DateTimeField(verbose_name=_('announcement timestamp'), auto_now_add=True)

    def send(self):
        if self.contest.push_announcements:
            event.post(f'contest_{self.contest.id_secret}', {
                'title': self.title,
                'message': self.description,
            })


class ContestParticipation(models.Model):
    LIVE = 0
    SPECTATE = -1

    contest = models.ForeignKey(Contest, verbose_name=_('associated contest'), related_name='users', on_delete=CASCADE)
    user = models.ForeignKey(Profile, verbose_name=_('user'), related_name='contest_history', on_delete=CASCADE)
    real_start = models.DateTimeField(verbose_name=_('start time'), default=timezone.now, db_column='start')
    score = models.FloatField(verbose_name=_('score'), default=0, db_index=True)
    cumtime = models.PositiveIntegerField(verbose_name=_('cumulative time'), default=0)
    frozen_score = models.FloatField(verbose_name=_('frozen score'), default=0, db_index=True,
                                     help_text=_('Frozen score in the scoreboard.'))
    frozen_cumtime = models.PositiveIntegerField(verbose_name=_('frozen cumulative time'), default=0,
                                                 help_text=_('Frozen cumulative time in the scoreboard.'))
    is_disqualified = models.BooleanField(verbose_name=_('is disqualified'), default=False,
                                          help_text=_('Whether this participation is disqualified.'))
    tiebreaker = models.FloatField(verbose_name=_('tie-breaking field'), default=0.0)
    frozen_tiebreaker = models.FloatField(verbose_name=_('frozen tie-breaking field'), default=0.0)
    virtual = models.IntegerField(verbose_name=_('virtual participation id'), default=LIVE,
                                  help_text=_('0 means non-virtual, otherwise the n-th virtual participation.'))
    format_data = JSONField(verbose_name=_('contest format specific data'), null=True, blank=True)

    def recompute_results(self):
        with transaction.atomic():
            self.contest.format.update_participation(self)
            if self.is_disqualified:
                self.score = -9999
                self.cumtime = 0
                self.tiebreaker = 0
                self.save(update_fields=['score', 'cumtime', 'tiebreaker'])
    recompute_results.alters_data = True

    def check_ban(self):
        if not settings.VNOJ_SHOULD_BAN_FOR_CHEATING_IN_CONTESTS or self.contest.is_organization_private:
            return

        disqualifications_count = ContestParticipation.objects.filter(
            user=self.user,
            contest__is_organization_private=False,
            is_disqualified=True,
        ).count()
        if disqualifications_count >= settings.VNOJ_MAX_DISQUALIFICATIONS_BEFORE_BANNING and \
                not self.user.is_banned:
            self.user.ban_user(settings.VNOJ_CONTEST_CHEATING_BAN_MESSAGE)
        elif disqualifications_count < settings.VNOJ_MAX_DISQUALIFICATIONS_BEFORE_BANNING and \
                self.user.is_banned and self.user.ban_reason == settings.VNOJ_CONTEST_CHEATING_BAN_MESSAGE:
            self.user.unban_user()
    check_ban.alters_data = True

    def set_disqualified(self, disqualified):
        self.is_disqualified = disqualified
        self.recompute_results()
        if self.contest.is_rated and self.contest.ratings.exists():
            self.contest.rate()
        if self.is_disqualified:
            if self.user.current_contest == self:
                self.user.remove_contest()
            self.contest.banned_users.add(self.user)
        else:
            self.contest.banned_users.remove(self.user)
        self.check_ban()
    set_disqualified.alters_data = True

    @property
    def live(self):
        return self.virtual == self.LIVE

    @property
    def spectate(self):
        return self.virtual == self.SPECTATE

    @cached_property
    def pre_registered(self):
        return self.real_start.astimezone(timezone.utc).date() == date(1970, 1, 1)

    @cached_property
    def start(self):
        contest = self.contest
        return contest.start_time if contest.time_limit is None and (self.live or self.spectate) else self.real_start

    @cached_property
    def end_time(self):
        contest = self.contest
        if self.spectate:
            return contest.end_time
        if self.virtual:
            if contest.time_limit:
                return self.real_start + contest.time_limit
            else:
                return self.real_start + (contest.end_time - contest.start_time)
        if self.pre_registered:
            return contest.end_time
        return contest.end_time if contest.time_limit is None else \
            min(self.real_start + contest.time_limit, contest.end_time)

    @cached_property
    def _now(self):
        # This ensures that all methods talk about the same now.
        return timezone.now()

    @property
    def ended(self):
        return self.end_time is not None and self.end_time < self._now

    @property
    def time_remaining(self):
        end = self.end_time
        if end is not None and end >= self._now:
            return end - self._now

    @property
    def is_frozen(self):
        return self.contest.is_frozen

    def __str__(self):
        if self.spectate:
            return _('%(user)s spectating in %(contest)s') % {'user': self.user.username, 'contest': self.contest.name}
        if self.virtual:
            return _('%(user)s in %(contest)s, v%(id)d') % {
                'user': self.user.username, 'contest': self.contest.name, 'id': self.virtual,
            }
        return _('%(user)s in %(contest)s') % {'user': self.user.username, 'contest': self.contest.name}

    class Meta:
        verbose_name = _('contest participation')
        verbose_name_plural = _('contest participations')

        unique_together = ('contest', 'user', 'virtual')


class ContestProblem(models.Model):
    problem = models.ForeignKey(Problem, verbose_name=_('problem'), related_name='contests', on_delete=CASCADE)
    contest = models.ForeignKey(Contest, verbose_name=_('contest'), related_name='contest_problems', on_delete=CASCADE)
    points = models.IntegerField(verbose_name=_('points'))
    partial = models.BooleanField(default=True, verbose_name=_('partial'))
    is_pretested = models.BooleanField(default=False, verbose_name=_('is pretested'))
    order = models.PositiveIntegerField(db_index=True, verbose_name=_('order'))
    output_prefix_override = models.IntegerField(verbose_name=_('output prefix length override'),
                                                 default=0, null=True, blank=True)
    max_submissions = models.IntegerField(help_text=_('Maximum number of submissions for this problem, '
                                                      'or leave blank for no limit.'),
                                          default=None, null=True, blank=True,
                                          validators=[MinValueOrNoneValidator(1, _('Why include a problem you '
                                                                                   "can't submit to?"))])

    class Meta:
        unique_together = ('problem', 'contest')
        verbose_name = _('contest problem')
        verbose_name_plural = _('contest problems')
        ordering = ('order',)


class ContestSubmission(models.Model):
    submission = models.OneToOneField(Submission, verbose_name=_('submission'),
                                      related_name='contest', on_delete=CASCADE)
    problem = models.ForeignKey(ContestProblem, verbose_name=_('problem'), on_delete=CASCADE,
                                related_name='submissions', related_query_name='submission')
    participation = models.ForeignKey(ContestParticipation, verbose_name=_('participation'), on_delete=CASCADE,
                                      related_name='submissions', related_query_name='submission')
    points = models.FloatField(default=0.0, verbose_name=_('points'))
    is_pretest = models.BooleanField(verbose_name=_('is pretested'),
                                     help_text=_('Whether this submission was ran only on pretests.'),
                                     default=False)

    class Meta:
        verbose_name = _('contest submission')
        verbose_name_plural = _('contest submissions')


class Rating(models.Model):
    user = models.ForeignKey(Profile, verbose_name=_('user'), related_name='ratings', on_delete=CASCADE)
    contest = models.ForeignKey(Contest, verbose_name=_('contest'), related_name='ratings', on_delete=CASCADE)
    participation = models.OneToOneField(ContestParticipation, verbose_name=_('participation'),
                                         related_name='rating', on_delete=CASCADE)
    rank = models.IntegerField(verbose_name=_('rank'))
    rating = models.IntegerField(verbose_name=_('rating'))
    mean = models.FloatField(verbose_name=_('raw rating'))
    performance = models.FloatField(verbose_name=_('contest performance'))
    last_rated = models.DateTimeField(db_index=True, verbose_name=_('last rated'))

    class Meta:
        unique_together = ('user', 'contest')
        verbose_name = _('contest rating')
        verbose_name_plural = _('contest ratings')


class ContestMoss(models.Model):
    LANG_MAPPING = [
        ('C', MOSS_LANG_C),
        ('C++', MOSS_LANG_CC),
        ('Java', MOSS_LANG_JAVA),
        ('Python', MOSS_LANG_PYTHON),
        ('Pascal', MOSS_LANG_PASCAL),
    ]

    contest = models.ForeignKey(Contest, verbose_name=_('contest'), related_name='moss', on_delete=CASCADE)
    problem = models.ForeignKey(Problem, verbose_name=_('problem'), related_name='moss', on_delete=CASCADE)
    language = models.CharField(max_length=10)
    submission_count = models.PositiveIntegerField(default=0)
    url = models.URLField(null=True, blank=True)

    class Meta:
        unique_together = ('contest', 'problem', 'language')
        verbose_name = _('contest moss result')
        verbose_name_plural = _('contest moss results')
