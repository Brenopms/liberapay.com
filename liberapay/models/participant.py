from __future__ import print_function, unicode_literals

from base64 import b64decode, b64encode
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from email.utils import formataddr
from hashlib import pbkdf2_hmac, md5
from os import urandom
from time import sleep
import uuid

from six.moves.urllib.parse import quote, urlencode

import aspen_jinja2_renderer
from html2text import html2text
import mangopay
from markupsafe import escape as htmlescape
from pando.utils import utcnow
from postgres.orm import Model
from psycopg2 import IntegrityError
from psycopg2.extras import Json

from liberapay.constants import (
    ASCII_ALLOWED_IN_USERNAME, AVATAR_QUERY, D_CENT, D_ZERO,
    DONATION_WEEKLY_MAX, DONATION_WEEKLY_MIN, EMAIL_RE,
    EMAIL_VERIFICATION_TIMEOUT, EVENTS,
    PASSWORD_MAX_SIZE, PASSWORD_MIN_SIZE, PERIOD_CONVERSION_RATES, PRIVILEGES,
    SESSION, SESSION_REFRESH, SESSION_TIMEOUT, USERNAME_MAX_SIZE
)
from liberapay.exceptions import (
    BadAmount,
    BadEmailAddress,
    BadPasswordSize,
    CannotRemovePrimaryEmail,
    EmailAlreadyAttachedToSelf,
    EmailAlreadyTaken,
    EmailNotVerified,
    NonexistingElsewhere,
    NoSelfTipping,
    NoTippee,
    TooManyEmailAddresses,
    TooManyEmailVerifications,
    TooManyPasswordLogins,
    TooManyUsernameChanges,
    UserDoesntAcceptTips,
    UsernameAlreadyTaken,
    UsernameBeginsWithRestrictedCharacter,
    UsernameContainsInvalidCharacters,
    UsernameIsEmpty,
    UsernameIsRestricted,
    UsernameTooLong,
    VerificationEmailAlreadySent,
)
from liberapay.models._mixin_team import MixinTeam
from liberapay.models.account_elsewhere import AccountElsewhere
from liberapay.models.community import Community
from liberapay.security.crypto import constant_time_compare
from liberapay.utils import (
    deserialize, erase_cookie, serialize, set_cookie,
    emails, i18n, markdown,
)
from liberapay.website import website


class Participant(Model, MixinTeam):

    typname = 'participants'

    ANON = False
    EMAIL_VERIFICATION_TIMEOUT = EMAIL_VERIFICATION_TIMEOUT

    def __eq__(self, other):
        if not isinstance(other, Participant):
            return False
        return self.id == other.id

    def __ne__(self, other):
        if not isinstance(other, Participant):
            return True
        return self.id != other.id

    def __repr__(self):
        return '<Participant #%s "%s">' % (repr(self.id), repr(self.username))


    # Constructors
    # ============

    @classmethod
    def make_stub(cls, cursor=None, **kw):
        """Return a new stub participant.
        """
        if kw:
            cols, vals = zip(*kw.items())
            cols = ', '.join(cols)
            placeholders = ', '.join(['%s']*len(vals))
            x = '({0}) VALUES ({1})'.format(cols, placeholders)
        else:
            x, vals = 'DEFAULT VALUES', ()
        with cls.db.get_cursor(cursor) as c:
            return c.one("""
                INSERT INTO participants {0}
                  RETURNING participants.*::participants
            """.format(x), vals)

    @classmethod
    def make_active(cls, kind, username=None, password=None, cursor=None):
        """Return a new active participant.
        """
        now = utcnow()
        d = {
            'kind': kind,
            'status': 'active',
            'join_time': now,
        }
        if password:
            d['password'] = cls.hash_password(password)
            d['password_mtime'] = now
        cols, vals = zip(*d.items())
        cols = ', '.join(cols)
        placeholders = ', '.join(['%s']*len(vals))
        with cls.db.get_cursor(cursor) as c:
            p = c.one("""
                INSERT INTO participants ({0}) VALUES ({1})
                  RETURNING participants.*::participants
            """.format(cols, placeholders), vals)
            if username:
                p.change_username(username, cursor=c)
        return p

    def make_team(self, name, email=None, email_lang=None, throttle_takes=True):
        if email and not self.email:
            email_is_attached_to_self = self.db.one("""
                SELECT true AS a
                  FROM emails
                 WHERE participant = %s
                   AND address = %s
            """, (self.id, email))
            if email_is_attached_to_self:
                raise EmailAlreadyAttachedToSelf(email)
        with self.db.get_cursor() as c:
            t = c.one("""
                INSERT INTO participants
                            (kind, status, join_time, throttle_takes)
                     VALUES ('group', 'active', now(), %s)
                  RETURNING participants.*::participants
            """, (throttle_takes,))
            t.change_username(name, cursor=c)
            t.add_member(self, c)
            if email:
                t.set_email_lang(email_lang, cursor=c)
                t.add_email(email, cursor=c)
        return t

    @classmethod
    def from_id(cls, id):
        """Return an existing participant based on id.
        """
        return cls._from_thing("id", id)

    @classmethod
    def from_username(cls, username):
        """Return an existing participant based on username.
        """
        return cls._from_thing("lower(username)", username.lower())

    @classmethod
    def _from_thing(cls, thing, value):
        assert thing in ("id", "lower(username)", "lower(email)")
        if thing == 'lower(email)':
            # This query looks for an unverified address if the participant
            # doesn't have any verified address
            return cls.db.one("""
                SELECT p.*::participants
                  FROM emails e
                  JOIN participants p ON p.id = e.participant
                 WHERE lower(e.address) = %s
                   AND (p.email IS NULL OR lower(p.email) = lower(e.address))
              ORDER BY p.email NULLS LAST, p.id ASC
                 LIMIT 1
            """, (value,))
        return cls.db.one("""
            SELECT participants.*::participants
              FROM participants
             WHERE {}=%s
        """.format(thing), (value,))

    @classmethod
    def from_mangopay_user_id(cls, mangopay_user_id):
        return cls.db.one("""
            SELECT p
              FROM mangopay_users u
              JOIN participants p ON p.id = u.participant
             WHERE u.id = %s
        """, (mangopay_user_id,))

    @classmethod
    def authenticate(cls, k1, k2, v1=None, v2=None):
        assert k1 in ('id', 'username', 'email')
        if not (v1 and v2):
            return
        if k1 in ('username', 'email'):
            k1 = 'lower(%s)' % k1
            v1 = v1.lower()
        p = cls._from_thing(k1, v1)
        if not p:
            return
        if k2 == 'session':
            if not p.session_token:
                return
            if p.session_expires < utcnow():
                return
            if constant_time_compare(p.session_token, v2):
                p.authenticated = True
                return p
        elif k2 == 'password':
            if not p.password:
                return
            cls.db.hit_rate_limit('log-in.password', p.id, TooManyPasswordLogins)
            algo, rounds, salt, hashed = p.password.split('$', 3)
            rounds = int(rounds)
            salt, hashed = b64decode(salt), b64decode(hashed)
            if constant_time_compare(cls._hash_password(v2, algo, salt, rounds), hashed):
                p.authenticated = True
                return p

    @classmethod
    def get_chargebacks_account(cls):
        r = cls.db.one("""
            SELECT p
              FROM participants p
             WHERE mangopay_user_id = 'CREDIT'
        """)
        if r:
            return r
        return cls.make_stub(
            goal=-1,
            hide_from_search=3,
            hide_from_lists=3,
            join_time=utcnow(),
            kind='organization',
            mangopay_user_id='CREDIT',
            mangopay_wallet_id='CREDIT_EUR',
            status='active',
            username='_chargebacks_',
        )

    def refetch(self):
        return self._from_thing('id', self.id)


    # Password Management
    # ===================

    @staticmethod
    def _hash_password(password, algo, salt, rounds):
        return pbkdf2_hmac(algo, password.encode('utf8'), salt, rounds)

    @classmethod
    def hash_password(cls, password):
        l = len(password)
        if l < PASSWORD_MIN_SIZE or l > PASSWORD_MAX_SIZE:
            raise BadPasswordSize
        algo = 'sha256'
        salt = urandom(21)
        rounds = website.app_conf.password_rounds
        hashed = cls._hash_password(password, algo, salt, rounds)
        hashed = '$'.join((
            algo,
            str(rounds),
            b64encode(salt).decode('ascii'),
            b64encode(hashed).decode('ascii')
        ))
        return hashed

    def update_password(self, password, cursor=None):
        hashed = self.hash_password(password)
        p_id = self.id
        with self.db.get_cursor(cursor) as c:
            c.run("""
                UPDATE participants
                   SET password = %(hashed)s
                     , password_mtime = CURRENT_TIMESTAMP
                 WHERE id = %(p_id)s;
            """, locals())


    # Session Management
    # ==================

    def update_session(self, new_token, expires):
        """Set ``session_token`` and ``session_expires``.
        """
        self.db.run("""
            UPDATE participants
               SET session_token=%s
                 , session_expires=%s
             WHERE id=%s
        """, (new_token, expires, self.id))
        self.set_attributes(session_token=new_token, session_expires=expires)

    def set_session_expires(self, expires):
        """Set ``session_expires`` to the given datetime.
        """
        self.db.run("UPDATE participants SET session_expires=%s WHERE id=%s",
                    (expires, self.id,))
        self.set_attributes(session_expires=expires)

    def start_session(self, suffix=''):
        """Start a new session for the user, invalidating the previous one.
        """
        token = uuid.uuid4().hex + suffix
        expires = utcnow() + SESSION_TIMEOUT
        self.update_session(token, expires)

    def sign_in(self, cookies, suffix=''):
        assert self.authenticated
        self.start_session(suffix)
        creds = '%s:%s' % (self.id, self.session_token)
        set_cookie(cookies, SESSION, creds, self.session_expires)

    def keep_signed_in(self, cookies):
        """Extend the user's current session.
        """
        new_expires = utcnow() + SESSION_TIMEOUT
        if new_expires - self.session_expires > SESSION_REFRESH:
            self.set_session_expires(new_expires)
            token = self.session_token
            creds = '%s:%s' % (self.id, token)
            set_cookie(cookies, SESSION, creds, expires=new_expires)

    def sign_out(self, cookies):
        """End the user's current session.
        """
        self.update_session(None, None)
        erase_cookie(cookies, SESSION)


    # Permissions
    # ===========

    def has_privilege(self, p):
        return self.privileges & PRIVILEGES[p]

    @property
    def is_admin(self):
        return self.privileges & PRIVILEGES['admin']


    # Statement
    # =========

    def get_statement(self, langs, type='profile'):
        """Get the participant's statement in the language that best matches
        the list provided, or the participant's "primary" statement if there
        are no matches. Returns a tuple `(content, lang)`.

        If langs isn't a list but a string, then it's assumed to be a language
        code and the corresponding statement content will be returned, or None.
        """
        p_id = self.id
        if not isinstance(langs, list):
            return self.db.one("""
                SELECT content
                  FROM statements
                 WHERE participant = %(p_id)s
                   AND type = %(type)s
                   AND lang = %(langs)s
            """, locals())
        return self.db.one("""
            SELECT content, lang
              FROM statements
         LEFT JOIN enumerate(%(langs)s::text[]) langs ON langs.value = statements.lang
             WHERE participant = %(p_id)s
               AND type = %(type)s
          ORDER BY langs.rank NULLS LAST, statements.id
             LIMIT 1
        """, locals(), default=(None, None))

    def get_statement_langs(self, type='profile'):
        return self.db.all("""
            SELECT lang FROM statements WHERE participant=%s AND type=%s
        """, (self.id, type))

    def upsert_statement(self, lang, statement, type='profile'):
        if not statement:
            self.db.run("""
                DELETE FROM statements
                 WHERE participant=%s
                   AND type=%s
                   AND lang=%s
            """, (self.id, type, lang))
            return
        search_conf = i18n.SEARCH_CONFS.get(lang, 'simple')
        self.db.run("""
            INSERT INTO statements
                        (lang, content, participant, search_conf, type, ctime, mtime)
                 VALUES (%s, %s, %s, %s, %s, now(), now())
            ON CONFLICT (participant, type, lang) DO UPDATE
                    SET content = excluded.content
                      , mtime = excluded.mtime
        """, (lang, statement, self.id, search_conf, type))


    # Stubs
    # =====

    def resolve_stub(self):
        rec = self.db.one("""
            SELECT platform, user_id, user_name, domain
              FROM elsewhere
             WHERE participant = %s
        """, (self.id,))
        if rec:
            if rec.user_name:
                slug = quote(rec.user_name) + ('@' + rec.domain if rec.domain else '')
            else:
                slug = '~' + quote(rec.user_id) + (':' + rec.domain if rec.domain else '')
            return '/on/%s/%s/' % (rec.platform, slug)
        return None


    # Closing
    # =======

    class AccountNotEmpty(Exception): pass

    def final_check(self, cursor):
        """Sanity-check that balance and tips have been dealt with.
        """
        if self.balance != 0:
            raise self.AccountNotEmpty
        incoming = cursor.one("""
            SELECT count(*) FROM current_tips WHERE tippee = %s AND amount > 0
        """, (self.id,))
        if incoming > 0:
            raise self.AccountNotEmpty

    class UnknownDisbursementStrategy(Exception): pass

    def close(self, disbursement_strategy):
        """Close the participant's account.
        """
        with self.db.get_cursor() as cursor:
            if disbursement_strategy is None:
                pass  # No balance, supposedly. final_check will make sure.
            elif disbursement_strategy == 'downstream':
                # This in particular needs to come before clear_tips_giving.
                self.distribute_balance_as_final_gift(cursor)
            else:
                raise self.UnknownDisbursementStrategy

            self.clear_tips_giving(cursor)
            self.clear_tips_receiving(cursor)
            self.clear_takes(cursor)
            if self.kind == 'group':
                self.remove_all_members(cursor)
            self.clear_personal_information(cursor)
            self.final_check(cursor)
            self.update_status('closed', cursor)

    class NoOneToGiveFinalGiftTo(Exception): pass

    def distribute_balance_as_final_gift(self, cursor):
        """Distribute a balance as a final gift.
        """
        if self.balance == 0:
            return

        tips = self.get_giving_for_profile()[0]
        tips = [t for t in tips if t.is_identified and not t.is_suspended]
        total = sum(t.amount for t in tips)
        transfers = []
        distributed = D_ZERO

        if not total:
            raise self.NoOneToGiveFinalGiftTo

        for tip in tips:
            rate = tip.amount / total
            pro_rated = (self.balance * rate).quantize(D_CENT, ROUND_DOWN)
            if pro_rated == 0:
                continue
            if tip.kind == 'group':
                team_id = tip.tippee
                team = Participant.from_id(team_id)
                takes = [
                    t for t in team.get_current_takes(cursor=cursor)
                    if t['is_identified'] and t['amount'] and t['member_id'] != self.id
                ]
                if not takes:
                    continue
                balance = pro_rated
                total_takes = sum(t['amount'] for t in takes)
                ratio = balance / total_takes if total_takes else 0
                for take in takes:
                    nominal = take['amount']
                    actual = min(
                        (nominal * ratio).quantize(D_CENT, rounding=ROUND_UP),
                        balance
                    )
                    if actual == 0:
                        continue
                    balance -= actual
                    transfers.append([take['member_id'], actual, team_id])
                assert balance == 0
            else:
                transfers.append([tip.tippee, pro_rated, None])
            distributed += pro_rated

        if not transfers:
            raise self.NoOneToGiveFinalGiftTo

        diff = self.balance - distributed
        if diff != 0:
            transfers[0][1] += diff  # Give it to the highest receiver.

        from liberapay.billing.transactions import transfer
        db = self.db
        tipper = self.id
        for tippee, amount, team in transfers:
            balance = transfer(db, tipper, tippee, amount, 'final-gift', team=team,
                               tipper_mango_id=self.mangopay_user_id,
                               tipper_wallet_id=self.mangopay_wallet_id)[0]

        assert balance == 0
        self.set_attributes(balance=balance)

    def clear_tips_giving(self, cursor):
        """Zero out tips from a given user.
        """
        tippees = cursor.all("""

            SELECT ( SELECT p.*::participants
                       FROM participants p
                      WHERE p.id=t.tippee
                    ) AS tippee
              FROM current_tips t
             WHERE tipper = %s
               AND amount > 0

        """, (self.id,))
        for tippee in tippees:
            self.set_tip_to(tippee, '0.00', update_self=False, cursor=cursor)

    def clear_tips_receiving(self, cursor):
        """Zero out tips to a given user.
        """
        tippers = cursor.all("""

            SELECT ( SELECT p.*::participants
                       FROM participants p
                      WHERE p.id=t.tipper
                    ) AS tipper
              FROM current_tips t
             WHERE tippee = %s
               AND amount > 0

        """, (self.id,))
        for tipper in tippers:
            tipper.set_tip_to(self, '0.00', update_tippee=False, cursor=cursor)

    def clear_takes(self, cursor):
        """Leave all teams by zeroing all takes.
        """
        teams = cursor.all("""
            SELECT p.*::participants
              FROM current_takes x
              JOIN participants p ON p.id = x.team
             WHERE member=%s
        """, (self.id,))
        for t in teams:
            t.set_take_for(self, None, self, cursor)

    def clear_personal_information(self, cursor):
        """Clear personal information such as statements and goal.
        """
        r = cursor.one("""

            DELETE FROM community_memberships WHERE participant=%(id)s;
            DELETE FROM subscriptions WHERE subscriber=%(id)s;
            DELETE FROM emails WHERE participant=%(id)s AND address <> %(email)s;
            DELETE FROM statements WHERE participant=%(id)s;

            UPDATE participants
               SET goal=NULL
                 , avatar_url=NULL
                 , session_token=NULL
                 , session_expires=now()
                 , giving=0
                 , receiving=0
                 , npatrons=0
             WHERE id=%(id)s
         RETURNING *;

        """, dict(id=self.id, email=self.email))
        self.set_attributes(**r._asdict())

    @property
    def closed_time(self):
        return self.db.one("""
            SELECT ts
              FROM events
             WHERE participant=%s
               AND type='set_status'
               AND payload='"closed"'
          ORDER BY ts DESC
             LIMIT 1
        """, (str(self.id),))


    # Emails
    # ======

    def add_email(self, email, cursor=None):
        """
            This is called when
            1) Adding a new email address
            2) Resending the verification email for an unverified email address

            Returns the number of emails sent.
        """

        # normalize the address: strip it, and lowercase the domain name
        email = email.strip()
        i = email.rfind('@')
        email = email[:i] + email[i:].lower()

        if not EMAIL_RE.match(email):
            raise BadEmailAddress(email)

        # Check that this address isn't already verified
        owner = (cursor or self.db).one("""
            SELECT participant
              FROM emails
             WHERE address = %(email)s
               AND verified IS true
        """, locals())
        if owner:
            if owner == self.id:
                return 0
            else:
                raise EmailAlreadyTaken(email)

        if len(self.get_emails()) > 9:
            raise TooManyEmailAddresses(email)

        with self.db.get_cursor(cursor) as c:
            self.add_event(c, 'add_email', email)
            email_row = c.one("""
                INSERT INTO emails AS e
                            (address, nonce, added_time, participant)
                     VALUES (%s, %s, current_timestamp, %s)
                ON CONFLICT (participant, address) DO UPDATE
                        SET added_time = excluded.added_time
                      WHERE e.verified IS NULL
                  RETURNING *
            """, (email, str(uuid.uuid4()), self.id))
            if not email_row:
                return 0
            # Limit number of verification emails per address
            self.db.hit_rate_limit('add_email.target', email, VerificationEmailAlreadySent)
            # Limit number of verification emails per participant
            self.db.hit_rate_limit('add_email.source', self.id, TooManyEmailVerifications)

        old_email = self.email or self.get_any_email()
        scheme = website.canonical_scheme
        host = website.canonical_host
        username = self.username
        addr_id = email_row.id
        nonce = email_row.nonce
        link = "{scheme}://{host}/{username}/emails/verify.html?email={addr_id}&nonce={nonce}"
        r = self.send_email('verification', email, link=link.format(**locals()), old_email=old_email)
        assert r == 1  # Make sure the verification email was sent

        if self.email:
            self.send_email('verification_notice', self.email, new_email=email)
            return 2
        else:
            self.update_avatar(cursor=cursor)

        return 1

    def update_email(self, email):
        if not getattr(self.get_email(email), 'verified', False):
            raise EmailNotVerified(email)
        id = self.id
        with self.db.get_cursor() as c:
            self.add_event(c, 'set_primary_email', email)
            c.run("""
                UPDATE participants
                   SET email=%(email)s
                 WHERE id=%(id)s
            """, locals())
        self.set_attributes(email=email)
        self.update_avatar()

    def verify_email(self, email, nonce):
        if '' in (email, nonce):
            return emails.VERIFICATION_MISSING
        if email.isdigit():
            r = self.db.one("""
                SELECT *
                  FROM emails
                 WHERE participant = %s
                   AND id = %s
            """, (self.id, email))
            email = r.address if r else None
        else:
            r = self.get_email(email)
        if r is None:
            return emails.VERIFICATION_FAILED
        if r.verified:
            assert r.nonce is None  # and therefore, order of conditions matters
            return emails.VERIFICATION_REDUNDANT
        if not constant_time_compare(r.nonce, nonce):
            return emails.VERIFICATION_FAILED
        if (utcnow() - r.added_time) > EMAIL_VERIFICATION_TIMEOUT:
            return emails.VERIFICATION_EXPIRED
        try:
            self.db.run("""
                UPDATE emails
                   SET verified=true, verified_time=now(), nonce=NULL
                 WHERE participant=%s
                   AND address=%s
                   AND verified IS NULL
            """, (self.id, email))
        except IntegrityError:
            return emails.VERIFICATION_STYMIED

        if not self.email:
            self.update_email(email)
        return emails.VERIFICATION_SUCCEEDED

    def get_email(self, email):
        return self.db.one("""
            SELECT *
              FROM emails
             WHERE participant=%s
               AND lower(address)=%s
        """, (self.id, email.lower()))

    def get_emails(self):
        return self.db.all("""
            SELECT *
              FROM emails
             WHERE participant=%s
          ORDER BY id
        """, (self.id,))

    def get_any_email(self, cursor=None):
        return (cursor or self.db).one("""
            SELECT address
              FROM emails
             WHERE participant=%s
             LIMIT 1
        """, (self.id,))

    def remove_email(self, address):
        if address == self.email:
            raise CannotRemovePrimaryEmail()
        with self.db.get_cursor() as c:
            self.add_event(c, 'remove_email', address)
            c.run("DELETE FROM emails WHERE participant=%s AND address=%s",
                  (self.id, address))
            n_left = c.one("SELECT count(*) FROM emails WHERE participant=%s", (self.id,))
            if n_left == 0:
                raise CannotRemovePrimaryEmail()

    def send_email(self, spt_name, email, **context):
        self.fill_notification_context(context)
        context['email'] = email
        langs = i18n.parse_accept_lang(self.email_lang or 'en')
        locale = i18n.match_lang(langs)
        i18n.add_helpers_to_context(context, locale)
        context['escape'] = lambda s: s
        context_html = dict(context)
        i18n.add_helpers_to_context(context_html, locale)
        context_html['escape'] = htmlescape
        spt = website.emails[spt_name]
        if spt_name == 'newsletter':
            def render(t, context):
                if t == 'text/html':
                    context['body'] = markdown.render(context['body']).strip()
                return spt[t].render(context).strip()
        else:
            base_spt = context.get('base_spt', 'base')
            base_spt = website.emails[base_spt] if base_spt else None
            bodies = {}
            def render(t, context):
                b = base_spt[t].render(context).strip() if base_spt else '$body'
                if t == 'text/plain' and t not in spt:
                    body = html2text(bodies['text/html']).strip()
                else:
                    body = spt[t].render(context).strip()
                bodies[t] = body
                return b.replace('$body', body)
        message = {}
        message['from_email'] = 'Liberapay Support <support@liberapay.com>'
        if spt_name == 'newsletter':
            message['from_email'] = 'Liberapay Newsletters <newsletters@liberapay.com>'
        message['to'] = [formataddr((self.username, email))]
        message['subject'] = spt['subject'].render(context).strip()
        message['html'] = render('text/html', context_html)
        message['text'] = render('text/plain', context)

        n = website.mailer.send(**message)
        website.log_email(message)
        return n

    @classmethod
    def dequeue_emails(cls):
        with cls.db.get_cursor() as cursor:
            cls._dequeue_emails(cursor)

    @classmethod
    def _dequeue_emails(cls, cursor):
        fetch_messages = lambda last_id: cursor.all("""
            SELECT *
              FROM notifications
             WHERE id > %s
               AND email AND email_sent IS NOT true
          ORDER BY id ASC
             LIMIT 60
        """, (last_id,))
        dequeue = lambda m: cls.db.run(
            "DELETE FROM notifications WHERE id = %s" if not m.web else
            "UPDATE notifications SET email_sent = true WHERE id = %s",
            (m.id,)
        )
        last_id = 0
        while True:
            messages = fetch_messages(last_id)
            if not messages:
                break
            for msg in messages:
                d = deserialize(msg.context)
                p = cls.from_id(msg.participant)
                email = d.get('email') or p.email
                if not email:
                    dequeue(msg)
                    continue
                try:
                    r = p.send_email(msg.event, email, **d)
                    assert r == 1
                except Exception as e:
                    website.tell_sentry(e, {})
                else:
                    dequeue(msg)
                sleep(1)
            last_id = messages[-1].id

    def set_email_lang(self, accept_lang, cursor=None):
        if not accept_lang:
            return
        if isinstance(accept_lang, bytes):
            accept_lang = accept_lang.decode('ascii', 'replace')
        (cursor or self.db).run(
            "UPDATE participants SET email_lang=%s WHERE id=%s",
            (accept_lang, self.id)
        )
        self.set_attributes(email_lang=accept_lang)


    # Notifications
    # =============

    def notify(self, event, force_email=False, email=True, web=True, **context):
        email = email and (force_email or self.email_notif_bits & EVENTS.get(event).bit > 0)
        p_id = self.id
        context = serialize(context)
        n_id = self.db.one("""
            INSERT INTO notifications
                        (participant, event, context, web, email)
                 VALUES (%(p_id)s, %(event)s, %(context)s, %(web)s, %(email)s)
              RETURNING id;
        """, locals())
        if not web:
            return n_id
        pending_notifs = self.db.one("""
            UPDATE participants
               SET pending_notifs = pending_notifs + 1
             WHERE id = %(p_id)s
         RETURNING pending_notifs;
        """, locals())
        self.set_attributes(pending_notifs=pending_notifs)
        return n_id

    def mark_notification_as_read(self, n_id):
        p_id = self.id
        r = self.db.one("""
            WITH updated AS (
                UPDATE notifications
                   SET is_new = false
                 WHERE participant = %(p_id)s
                   AND id = %(n_id)s
                   AND is_new
                   AND web
             RETURNING id
            )
            UPDATE participants
               SET pending_notifs = pending_notifs - (SELECT count(*) FROM updated)
             WHERE id = %(p_id)s
         RETURNING pending_notifs;
        """, locals())
        self.set_attributes(pending_notifs=r)

    def mark_notifications_as_read(self, event=None, until=None):
        if not self.pending_notifs:
            return
        p_id = self.id
        sql_filter = 'AND event = %(event)s' if event else ''

        if until:
            sql_filter += ' AND id <= %(until)s'

        r = self.db.one("""
            WITH updated AS (
                UPDATE notifications
                   SET is_new = false
                 WHERE participant = %(p_id)s
                   AND is_new
                   AND web
                   {0}
             RETURNING id
            )
            UPDATE participants
               SET pending_notifs = pending_notifs - (SELECT count(*) FROM updated)
             WHERE id = %(p_id)s
         RETURNING pending_notifs;
        """.format(sql_filter), locals())
        self.set_attributes(pending_notifs=r)

    def remove_notification(self, n_id):
        p_id = self.id
        r = self.db.one("""
            WITH deleted AS (
                DELETE FROM notifications
                 WHERE id = %(n_id)s
                   AND participant = %(p_id)s
                   AND web
             RETURNING is_new
            )
            UPDATE participants
               SET pending_notifs = pending_notifs - (
                       SELECT count(*) FROM deleted WHERE is_new
                   )
             WHERE id = %(p_id)s
         RETURNING pending_notifs;
        """, locals())
        self.set_attributes(pending_notifs=r)

    def fill_notification_context(self, context):
        context.update(aspen_jinja2_renderer.Renderer.global_context)
        context['participant'] = self
        context['username'] = self.username
        context['button_style'] = lambda variant: (
            "color: {text_color}; text-decoration: none; display: inline-block; "
            "padding: 0 16px; background: {bg_color}; white-space: nowrap; "
            "border: 1px solid {border_color}; border-radius: 3px; "
            "font: normal 16px/40px Ubuntu, Verdana, sans-serif;"
        ).format(
            bg_color=website.scss_variables['btn-' + variant + '-bg'],
            border_color=website.scss_variables['btn-' + variant + '-border'],
            text_color=website.scss_variables['btn-' + variant + '-color'],
        )

    def get_notifs(self):
        return self.db.all("""
            SELECT id, event, context, is_new, ts
              FROM notifications
             WHERE participant = %s
               AND web
          ORDER BY is_new DESC, id DESC
        """, (self.id,))

    def render_notifications(self, state, notifs=None):
        """Render notifications as HTML.

        The `notifs` argument allows rendering arbitrary notifications.

        """
        notifs = notifs or self.get_notifs()

        r = []
        for id, event, notif_context, is_new, ts in notifs:
            try:
                notif_context = deserialize(notif_context)
                context = dict(state)
                self.fill_notification_context(context)
                context.update(notif_context)
                spt = website.emails[event]
                subject = spt['subject'].render(context).strip()
                html = spt['text/html'].render(context).strip()
                typ = notif_context.get('type', 'info')
                r.append(dict(id=id, subject=subject, html=html, type=typ, is_new=is_new, ts=ts))
            except Exception as e:
                website.tell_sentry(e, state)
        return r

    def notify_patrons(self, elsewhere, tips):
        for t in tips:
            Participant.from_id(t.tipper).notify(
                'pledgee_joined',
                user_name=elsewhere.user_name,
                platform=elsewhere.platform_data.display_name,
                amount=t.amount,
                profile_url=elsewhere.liberapay_url,
            )


    # Exchange-related stuff
    # ======================

    @property
    def withdrawable_balance(self):
        from liberapay.billing.transactions import QUARANTINE
        return self.db.one("""
            SELECT COALESCE(sum(amount), 0)
              FROM cash_bundles
             WHERE owner = %s
               AND ts < now() - INTERVAL %s
               AND disputed IS NOT TRUE
               AND locked_for IS NULL
        """, (self.id, QUARANTINE))


    # Events
    # ======

    def add_event(self, c, type, payload, recorder=None):
        return c.one("""
            INSERT INTO events
                        (participant, type, payload, recorder)
                 VALUES (%s, %s, %s, %s)
              RETURNING *
        """, (self.id, type, Json(payload), recorder))

    def get_last_event_of_type(self, type):
        return self.db.one("""
            SELECT *
              FROM events
             WHERE participant = %s
               AND type = %s
          ORDER BY ts DESC
             LIMIT 1
        """, (self.id, type))


    # Newsletters
    # ===========

    def upsert_subscription(self, on, publisher):
        subscriber = self.id
        token = str(uuid.uuid4()) if on else None
        r = self.db.one("""
            DO $$
            DECLARE
                cname text;
            BEGIN
                IF (%(on)s) THEN
                BEGIN
                    INSERT INTO subscriptions
                                (publisher, subscriber, is_on, token)
                         VALUES (%(publisher)s, %(subscriber)s, %(on)s, %(token)s);
                    IF (FOUND) THEN RETURN; END IF;
                EXCEPTION WHEN unique_violation THEN
                    GET STACKED DIAGNOSTICS cname = CONSTRAINT_NAME;
                    IF (cname <> 'subscriptions_publisher_subscriber_key') THEN
                        RAISE;
                    END IF;
                END;
                END IF;
                UPDATE subscriptions
                   SET is_on = %(on)s
                     , mtime = CURRENT_TIMESTAMP
                 WHERE publisher = %(publisher)s
                   AND subscriber = %(subscriber)s;
            END;
            $$ LANGUAGE plpgsql;

            SELECT *
              FROM subscriptions
             WHERE publisher = %(publisher)s
               AND subscriber = %(subscriber)s;
        """, locals())
        if not r and on:
            raise Exception('upsert in subscriptions failed')
        return r

    def check_subscription_status(self, subscriber):
        return self.db.one("""
            SELECT is_on
              FROM subscriptions
             WHERE publisher = %s AND subscriber = %s
        """, (self.id, subscriber.id))

    @classmethod
    def get_subscriptions(cls, publisher):
        unsub_url = '{}/~{}/unsubscribe?id=%s&token=%s'.format(website.canonical_url, publisher)
        return cls.db.all("""
            SELECT s.*
                 , format(%(unsub_url)s, s.id, s.token) AS unsubscribe_url
              FROM subscriptions s
             WHERE s.publisher = %(publisher)s
        """, locals())

    @classmethod
    def send_newsletters(cls):
        fetch_messages = lambda: cls.db.all("""
            SELECT n.sender
                 , row_to_json((SELECT a FROM (
                        SELECT t.newsletter, t.lang, t.subject, t.body
                   ) a)) AS context
              FROM newsletter_texts t
              JOIN newsletters n ON n.id = t.newsletter
             WHERE scheduled_for <= now() + INTERVAL '30 seconds'
               AND sent_at IS NULL
          ORDER BY scheduled_for ASC
        """)
        while True:
            messages = fetch_messages()
            if not messages:
                break
            for msg in messages:
                with cls.db.get_cursor() as cursor:
                    count = 0
                    for s in cls.get_subscriptions(msg.sender):
                        context = dict(msg.context, unsubscribe_url=s.unsubscribe_url)
                        count += cursor.one("""
                            INSERT INTO notifications
                                        (participant, event, context, web, email)
                                 SELECT p.id, 'newsletter', %s, false, true
                                   FROM participants p
                                  WHERE p.id = %s
                                    AND p.email IS NOT NULL
                         RETURNING count(*)
                        """, (serialize(context), s.subscriber))
                    assert cursor.one("""
                        UPDATE newsletter_texts
                           SET sent_at = now()
                             , sent_count = %s
                         WHERE id = %s
                     RETURNING sent_at
                    """, (count, msg.id))
                sleep(1)


    # Random Stuff
    # ============

    def url(self, path='', query=''):
        scheme = website.canonical_scheme
        host = website.canonical_host
        username = self.username
        if query:
            assert '?' not in path
            query = '?' + urlencode(query)
        return '{scheme}://{host}/{username}/{path}{query}'.format(**locals())

    def get_teams(self):
        """Return a list of teams this user is a member of.
        """
        return self.db.all("""

            SELECT team AS id
                 , p.username AS name
                 , p.avatar_url
                 , ( SELECT count(*)
                       FROM current_takes
                      WHERE team=x.team
                    ) AS nmembers
              FROM current_takes x
              JOIN participants p ON p.id = x.team
             WHERE member=%s;

        """, (self.id,))

    @property
    def accepts_tips(self):
        return (self.goal is None) or (self.goal >= 0)


    # Communities
    # ===========

    def create_community(self, name, **kw):
        return Community.create(name, self.id, **kw)

    def upsert_community_membership(self, on, c_id):
        p_id = self.id
        self.db.run("""
            DO $$
            DECLARE
                cname text;
            BEGIN
                BEGIN
                    INSERT INTO community_memberships
                                (community, participant, is_on)
                         VALUES (%(c_id)s, %(p_id)s, %(on)s);
                    IF (FOUND) THEN RETURN; END IF;
                EXCEPTION WHEN unique_violation THEN
                    GET STACKED DIAGNOSTICS cname = CONSTRAINT_NAME;
                    IF (cname <> 'community_memberships_participant_community_key') THEN
                        RAISE;
                    END IF;
                END;
                UPDATE community_memberships
                   SET is_on = %(on)s
                     , mtime = CURRENT_TIMESTAMP
                 WHERE community = %(c_id)s
                   AND participant = %(p_id)s;
                IF (NOT FOUND) THEN
                    RAISE 'upsert in community_memberships failed';
                END IF;
            END;
            $$ LANGUAGE plpgsql;
        """, locals())

    def get_communities(self):
        return self.db.all("""
            SELECT c.*, replace(c.name, '_', ' ') AS pretty_name
              FROM community_memberships cm
              JOIN communities c ON c.id = cm.community
             WHERE cm.is_on AND cm.participant = %s
          ORDER BY c.nmembers ASC, c.name
        """, (self.id,))


    # Invoices
    # ========

    def can_invoice(self, other):
        if self.kind != 'individual' or other.kind != 'organization':
            return False
        return bool(self.allow_invoices and other.allow_invoices)

    def update_invoice_status(self, invoice_id, new_status, message=None):
        if new_status in ('canceled', 'new', 'retracted'):
            column = 'sender'
        elif new_status in ('accepted', 'paid', 'rejected'):
            column = 'addressee'
        else:
            raise ValueError(new_status)
        if new_status in ('new', 'canceled'):
            old_status = 'pre'
        elif new_status == 'paid':
            old_status = 'accepted'
        else:
            old_status = 'new'
        with self.db.get_cursor() as c:
            p_id = self.id
            r = c.one("""
                UPDATE invoices
                   SET status = %(new_status)s
                 WHERE id = %(invoice_id)s
                   AND status = %(old_status)s
                   AND {0} = %(p_id)s
             RETURNING id
            """.format(column), locals())
            if not r:
                return False
            c.run("""
                INSERT INTO invoice_events
                            (invoice, participant, status, message)
                     VALUES (%s, %s, %s, %s)
            """, (invoice_id, self.id, new_status, message))
        return True

    def pay_invoice(self, invoice):
        assert self.id == invoice.addressee
        if self.balance < invoice.amount:
            return False
        from liberapay.billing.transactions import transfer
        balance = transfer(
            self.db, self.id, invoice.sender, invoice.amount, invoice.nature,
            invoice=invoice.id,
            tipper_mango_id=self.mangopay_user_id,
            tipper_wallet_id=self.mangopay_wallet_id,
        )[0]
        self.update_invoice_status(invoice.id, 'paid')
        self.set_attributes(balance=balance)
        return True


    # More Random Stuff
    # =================

    def change_username(self, suggested, cursor=None, recorder=None):
        suggested = suggested and suggested.strip()

        if not suggested:
            raise UsernameIsEmpty(suggested)

        if len(suggested) > USERNAME_MAX_SIZE:
            raise UsernameTooLong(suggested)

        if set(suggested) - ASCII_ALLOWED_IN_USERNAME:
            raise UsernameContainsInvalidCharacters(suggested)

        if suggested[0] == '.':
            raise UsernameBeginsWithRestrictedCharacter(suggested)

        lowercased = suggested.lower()

        if lowercased in website.restricted_usernames:
            raise UsernameIsRestricted(suggested)

        if suggested != self.username:
            with self.db.get_cursor(cursor) as c:
                try:
                    # Will raise IntegrityError if the desired username is taken.
                    actual = c.one("""
                        UPDATE participants
                           SET username=%s
                         WHERE id=%s
                           AND username <> %s
                     RETURNING username, lower(username)
                    """, (suggested, self.id, suggested))
                except IntegrityError:
                    raise UsernameAlreadyTaken(suggested)
                if actual is None:
                    return suggested
                assert (suggested, lowercased) == actual  # sanity check

                # Deal with redirections
                last_rename = self.get_last_event_of_type('set_username')
                if last_rename:
                    c.hit_rate_limit('change_username', self.id, TooManyUsernameChanges)
                    old_username = last_rename.payload
                    prefixes = {
                        'old': '/%s/' % old_username.lower(),
                        'new': '/%s/' % suggested.lower(),
                    }
                    # Delete and update previous redirections
                    c.run("""
                        DELETE FROM redirections WHERE from_prefix = %(new)s || '%%';
                        UPDATE redirections
                           SET to_prefix = %(new)s
                             , mtime = now()
                         WHERE to_prefix = %(old)s;
                    """, prefixes)
                    if prefixes['old'] != prefixes['new']:
                        # Add a redirection if the old name was in use long enough (1 hour)
                        active_period = utcnow() - last_rename.ts
                        if active_period.total_seconds() > 3600:
                            c.run("""
                                INSERT INTO redirections
                                            (from_prefix, to_prefix)
                                     VALUES (%(old)s || '%%', %(new)s)
                                ON CONFLICT (from_prefix) DO UPDATE
                                        SET to_prefix = excluded.to_prefix
                                          , mtime = now()
                            """, prefixes)

                self.add_event(c, 'set_username', suggested)
                self.set_attributes(username=suggested)

            if last_rename and self.kind == 'group':
                assert isinstance(recorder, Participant)
                members = self.db.all("""
                    SELECT p
                      FROM current_takes t
                      JOIN participants p ON p.id = t.member
                     WHERE t.team = %s
                """, (self.id,))
                for m in members:
                    if m != recorder:
                        m.notify(
                            'team_rename', email=False, web=True,
                            old_name=old_username, new_name=suggested,
                            renamed_by=recorder.username,
                        )

        return suggested

    def update_avatar(self, src=None, cursor=None):
        if self.status == 'stub':
            assert src is None

        src = self.avatar_src if src is None else src
        platform, key = src.split(':', 1) if src else (None, None)
        email = self.avatar_email or self.email or self.get_any_email(cursor)

        if platform == 'libravatar' or platform is None and email:
            if not email:
                return
            avatar_id = md5(email.strip().lower().encode('utf8')).hexdigest()
            avatar_url = 'https://seccdn.libravatar.org/avatar/'+avatar_id
            avatar_url += AVATAR_QUERY

        elif platform is None:
            avatar_url = (cursor or self.db).one("""
                SELECT avatar_url
                  FROM elsewhere
                 WHERE participant = %s
              ORDER BY platform = 'github' DESC,
                       avatar_url LIKE '%%libravatar.org%%' DESC,
                       avatar_url LIKE '%%gravatar.com%%' DESC
                 LIMIT 1
            """, (self.id,))

        else:
            avatar_url = (cursor or self.db).one("""
                SELECT avatar_url
                  FROM elsewhere
                 WHERE participant = %s
                   AND platform = %s
                -- AND user_id = %%s  -- not implemented yet
            """, (self.id, platform))

        if not avatar_url:
            return

        (cursor or self.db).run("""
            UPDATE participants
               SET avatar_url = %s
                 , avatar_src = %s
             WHERE id = %s
        """, (avatar_url, src, self.id))
        self.set_attributes(avatar_src=src, avatar_url=avatar_url)

        return avatar_url

    def update_goal(self, goal, cursor=None):
        with self.db.get_cursor(cursor) as c:
            json = None if goal is None else str(goal)
            self.add_event(c, 'set_goal', json)
            c.run("UPDATE participants SET goal=%s WHERE id=%s", (goal, self.id))
            self.set_attributes(goal=goal)
            if not self.accepts_tips:
                self.clear_tips_receiving(c)
                self.update_receiving(c)

    def update_status(self, status, cursor=None):
        with self.db.get_cursor(cursor) as c:
            goal = 'goal'
            if status == 'closed':
                goal = '-1'
            elif status == 'active':
                goal = 'NULL'
            r = c.one("""
                UPDATE participants
                   SET status = %(status)s
                     , join_time = COALESCE(join_time, CURRENT_TIMESTAMP)
                     , goal = {0}
                 WHERE id=%(id)s
             RETURNING status, join_time, goal
            """.format(goal), dict(id=self.id, status=status))
            self.set_attributes(**r._asdict())
            self.add_event(c, 'set_status', status)
            if not self.accepts_tips:
                self.clear_tips_receiving(c)
                self.update_receiving(c)

    def update_giving_and_tippees(self, cursor):
        updated_tips = self.update_giving(cursor)
        for tip in updated_tips:
            Participant.from_id(tip.tippee).update_receiving(cursor)

    def update_giving(self, cursor=None):
        # Update is_funded on tips
        tips = (cursor or self.db).all("""
            SELECT t.*
              FROM current_tips t
              JOIN participants p2 ON p2.id = t.tippee
             WHERE t.tipper = %s
               AND t.amount > 0
          ORDER BY p2.join_time IS NULL, t.ctime ASC
        """, (self.id,))
        fake_balance = self.balance + self.receiving
        updated = []
        for tip in tips:
            if tip.amount > fake_balance:
                is_funded = False
            else:
                fake_balance -= tip.amount
                is_funded = True
            if tip.is_funded == is_funded:
                continue
            updated.append((cursor or self.db).one("""
                UPDATE tips
                   SET is_funded = %s
                 WHERE id = %s
             RETURNING *
            """, (is_funded, tip.id)))

        # Update giving on participant
        giving = (cursor or self.db).one("""
            UPDATE participants p
               SET giving = COALESCE((
                     SELECT sum(amount)
                       FROM current_tips
                       JOIN participants p2 ON p2.id = tippee
                      WHERE tipper = %(id)s
                        AND p2.status = 'active'
                        AND (p2.mangopay_user_id IS NOT NULL OR kind = 'group')
                        AND amount > 0
                        AND is_funded
                   ), 0)
             WHERE p.id = %(id)s
         RETURNING giving
        """, dict(id=self.id))
        self.set_attributes(giving=giving)

        return updated

    def update_receiving(self, cursor=None):
        with self.db.get_cursor(cursor) as c:
            if self.kind == 'group':
                c.run("LOCK TABLE takes IN EXCLUSIVE MODE")
            r = c.one("""
                WITH our_tips AS (
                         SELECT amount
                           FROM current_tips
                          WHERE tippee = %(id)s
                            AND amount > 0
                            AND is_funded
                     )
                UPDATE participants p
                   SET receiving = (COALESCE((
                           SELECT sum(amount)
                             FROM our_tips
                       ), 0) + taking)
                     , npatrons = COALESCE((SELECT count(*) FROM our_tips), 0)
                 WHERE p.id = %(id)s
             RETURNING receiving, npatrons
            """, dict(id=self.id))
            self.set_attributes(receiving=r.receiving, npatrons=r.npatrons)
            if self.kind == 'group':
                self.recompute_actual_takes(c)


    def set_tip_to(self, tippee, periodic_amount, period='weekly',
                   update_self=True, update_tippee=True, cursor=None):
        """Given a Participant or username, and amount as str, returns a dict.

        We INSERT instead of UPDATE, so that we have history to explore. The
        COALESCE function returns the first of its arguments that is not NULL.
        The effect here is to stamp all tips with the timestamp of the first
        tip from this user to that. I believe this is used to determine the
        order of transfers during payday.

        The dict returned represents the row inserted in the tips table, with
        an additional boolean indicating whether this is the first time this
        tipper has tipped (we want to track that as part of our conversion
        funnel).

        """
        assert self.status == 'active'  # sanity check

        if isinstance(tippee, AccountElsewhere):
            tippee = tippee.participant
        elif not isinstance(tippee, Participant):
            tippee, u = Participant.from_username(tippee), tippee
            if not tippee:
                raise NoTippee(u)

        if self.id == tippee.id:
            raise NoSelfTipping

        periodic_amount = Decimal(periodic_amount)  # May raise InvalidOperation
        amount = periodic_amount * PERIOD_CONVERSION_RATES[period]

        if periodic_amount != 0 and amount < DONATION_WEEKLY_MIN or amount > DONATION_WEEKLY_MAX:
            raise BadAmount(periodic_amount, period)

        amount = amount.quantize(D_CENT, rounding=ROUND_UP)

        if not tippee.accepts_tips and amount != 0:
            raise UserDoesntAcceptTips(tippee.username)

        # Insert tip
        t = (cursor or self.db).one("""\

            INSERT INTO tips
                        (ctime, tipper, tippee, amount, period, periodic_amount)
                 VALUES ( COALESCE (( SELECT ctime
                                        FROM tips
                                       WHERE (tipper=%(tipper)s AND tippee=%(tippee)s)
                                       LIMIT 1
                                      ), CURRENT_TIMESTAMP)
                        , %(tipper)s, %(tippee)s, %(amount)s, %(period)s, %(periodic_amount)s
                         )
              RETURNING *
                      , ( SELECT count(*) = 0 FROM tips WHERE tipper=%(tipper)s ) AS first_time_tipper
                      , ( SELECT join_time IS NULL FROM participants WHERE id = %(tippee)s ) AS is_pledge

        """, dict(tipper=self.id, tippee=tippee.id, amount=amount,
                  period=period, periodic_amount=periodic_amount))._asdict()

        if update_self:
            # Update giving amount of tipper
            updated = self.update_giving(cursor)
            for u in updated:
                if u.id == t['id']:
                    t['is_funded'] = u.is_funded
        if update_tippee:
            # Update receiving amount of tippee
            tippee.update_receiving(cursor)

        return t


    @staticmethod
    def _zero_tip_dict(tippee):
        if isinstance(tippee, Participant):
            tippee = tippee.id
        return dict(amount=D_ZERO, is_funded=False, tippee=tippee,
                    period='weekly', periodic_amount=D_ZERO)


    def get_tip_to(self, tippee):
        """Given a participant (or their id), returns a dict.
        """
        default = self._zero_tip_dict(tippee)
        tippee = default['tippee']
        if self.id == tippee:
            return default
        return self.db.one("""\

            SELECT *
              FROM tips
             WHERE tipper=%s
               AND tippee=%s
          ORDER BY mtime DESC
             LIMIT 1

        """, (self.id, tippee), back_as=dict, default=default)


    def get_tip_distribution(self):
        """
            Returns a data structure in the form of::

                [
                    [TIPAMOUNT1, TIPAMOUNT2...TIPAMOUNTN],
                    total_number_patrons_giving_to_me,
                    total_amount_received
                ]

            where each TIPAMOUNTN is in the form::

                [
                    amount,
                    number_of_tippers_for_this_amount,
                    total_amount_given_at_this_amount,
                    proportion_of_tips_at_this_amount,
                    proportion_of_total_amount_at_this_amount
                ]

        """
        SQL = """

            SELECT amount
                 , count(amount) AS ncontributing
              FROM ( SELECT DISTINCT ON (tipper)
                            amount
                          , tipper
                       FROM tips
                      WHERE tippee=%s
                        AND is_funded
                   ORDER BY tipper
                          , mtime DESC
                    ) AS foo
             WHERE amount > 0
          GROUP BY amount
          ORDER BY amount

        """

        tip_amounts = []

        npatrons = 0.0  # float to trigger float division
        contributed = D_ZERO
        for rec in self.db.all(SQL, (self.id,)):
            tip_amounts.append([
                rec.amount,
                rec.ncontributing,
                rec.amount * rec.ncontributing,
            ])
            contributed += tip_amounts[-1][2]
            npatrons += rec.ncontributing

        for row in tip_amounts:
            row.append((row[1] / npatrons) if npatrons > 0 else 0)
            row.append((row[2] / contributed) if contributed > 0 else 0)

        return tip_amounts, npatrons, contributed


    def get_giving_for_profile(self):

        tips = self.db.all("""\

            SELECT * FROM (
                SELECT DISTINCT ON (tippee)
                       amount
                     , period
                     , periodic_amount
                     , tippee
                     , t.ctime
                     , t.mtime
                     , p.join_time
                     , p.username
                     , p.kind
                     , t.is_funded
                     , (p.mangopay_user_id IS NOT NULL OR kind = 'group') AS is_identified
                     , p.is_suspended
                  FROM tips t
                  JOIN participants p ON p.id = t.tippee
                 WHERE tipper = %s
                   AND p.status = 'active'
              ORDER BY tippee
                     , t.mtime DESC
            ) AS foo
            ORDER BY amount DESC
                   , username

        """, (self.id,))

        pledges = self.db.all("""\

            SELECT * FROM (
                SELECT DISTINCT ON (tippee)
                       amount
                     , period
                     , periodic_amount
                     , tippee
                     , t.ctime
                     , t.mtime
                     , (e, p)::elsewhere_with_participant AS e_account
                  FROM tips t
                  JOIN participants p ON p.id = t.tippee
                  JOIN elsewhere e ON e.participant = t.tippee
                 WHERE tipper = %s
                   AND p.status = 'stub'
              ORDER BY tippee
                     , t.mtime DESC
            ) AS foo
            ORDER BY amount DESC
                   , ctime DESC

        """, (self.id,))


        # Compute the total

        total = sum([t.amount for t in tips])
        if not total:
            # If tips is an empty list, total is int 0. We want a Decimal.
            total = D_ZERO

        pledges_total = sum([t.amount for t in pledges])
        if not pledges_total:
            pledges_total = D_ZERO

        return tips, total, pledges, pledges_total

    def get_tips_receiving(self):
        return self.db.all("""
            SELECT *
              FROM current_tips
             WHERE tippee=%s
               AND amount>0
        """, (self.id,))

    def get_current_tips(self):
        """Get the tips this participant is currently sending to others.
        """
        return self.db.all("""
            SELECT * FROM (
                SELECT DISTINCT ON (tippee)
                       amount
                     , period
                     , periodic_amount
                     , tippee
                     , t.ctime
                     , p.username
                     , p.join_time
                  FROM tips t
                  JOIN participants p ON p.id = t.tippee
                 WHERE tipper = %s
              ORDER BY tippee
                     , t.mtime DESC
            ) AS foo
            ORDER BY amount DESC
                   , tippee
        """, (self.id,), back_as=dict)


    def get_age_in_seconds(self):
        if self.join_time is not None:
            return (utcnow() - self.join_time).total_seconds()
        return -1


    def get_mangopay_account(self):
        """Fetch the mangopay account for this participant.
        """
        if not self.mangopay_user_id:
            return
        return mangopay.resources.User.get(self.mangopay_user_id)


    # Accounts Elsewhere
    # ==================

    def get_account_elsewhere(self, platform):
        """Return an AccountElsewhere instance.
        """
        return self.db.one("""

            SELECT elsewhere.*::elsewhere_with_participant
              FROM elsewhere
             WHERE participant=%s
               AND platform=%s

        """, (self.id, platform))

    def get_accounts_elsewhere(self):
        """Return a dict of AccountElsewhere instances.
        """
        accounts = self.db.all("""

            SELECT (e, p)::elsewhere_with_participant
              FROM elsewhere e
              JOIN participants p ON p.id = e.participant
             WHERE e.participant = %s

        """, (self.id,))
        accounts_dict = {account.platform: account for account in accounts}
        return accounts_dict

    def take_over(self, account, have_confirmation=False):
        """Given an AccountElsewhere or a tuple (platform_name, domain, user_id),
        associate an elsewhere account.

        Returns None or raises NeedConfirmation.

        This method associates an account on another platform (GitHub, Twitter,
        etc.) with the given Liberapay participant. Every account elsewhere has an
        associated Liberapay participant account, even if its only a stub
        participant (it allows us to track pledges to that account should they
        ever decide to join Liberapay).

        In certain circumstances, we want to present the user with a
        confirmation before proceeding to transfer the account elsewhere to
        the new Liberapay account; NeedConfirmation is the signal to request
        confirmation.
        """

        if isinstance(account, AccountElsewhere):
            platform, domain, user_id = account.platform, account.domain, account.user_id
        else:
            platform, domain, user_id = map(str, account)

        CREATE_TEMP_TABLE_FOR_TIPS = """
            CREATE TEMP TABLE temp_tips ON COMMIT drop AS
                SELECT ctime, tipper, tippee, amount, period, periodic_amount, is_funded
                  FROM current_tips
                 WHERE (tippee = %(dead)s OR tippee = %(live)s)
                   AND amount > 0;
        """

        CONSOLIDATE_TIPS_RECEIVING = """
            -- Create a new set of tips, one for each current tip *to* either
            -- the dead or the live account. If a user was tipping both the
            -- dead and the live account, then we keep the highest tip. We don't
            -- sum the amounts to prevent the new one from being above the
            -- maximum allowed.
            INSERT INTO tips (ctime, tipper, tippee, amount, period, periodic_amount, is_funded)
                 SELECT DISTINCT ON (tipper)
                        ctime, tipper, %(live)s AS tippee, amount, period,
                        periodic_amount, is_funded
                   FROM temp_tips
                  WHERE (tippee = %(dead)s OR tippee = %(live)s)
                        -- Include tips *to* either the dead or live account.
                AND NOT (tipper = %(dead)s OR tipper = %(live)s)
                        -- Don't include tips *from* the dead or live account,
                        -- lest we convert cross-tipping to self-tipping.
               ORDER BY tipper, amount DESC
        """

        ZERO_OUT_OLD_TIPS_RECEIVING = """
            INSERT INTO tips (ctime, tipper, tippee, amount, period, periodic_amount)
                SELECT ctime, tipper, tippee, 0 AS amount, period, 0 AS periodic_amount
                  FROM temp_tips
                 WHERE tippee=%s
        """

        with self.db.get_cursor() as cursor:

            # Load the existing connection
            # Every account elsewhere has at least a stub participant account
            # on Liberapay.
            elsewhere = cursor.one("""
                SELECT (e, p)::elsewhere_with_participant
                  FROM elsewhere e
                  JOIN participants p ON p.id = e.participant
                 WHERE e.platform=%s AND e.domain=%s AND e.user_id=%s
            """, (platform, domain, user_id), default=Exception)
            other = elsewhere.participant

            if self.id == other.id:
                # this is a no op - trying to take over itself
                return

            # Save old tips so we can notify patrons that they've been claimed
            old_tips = other.get_tips_receiving() if other.status == 'stub' else None

            # Make sure we have user confirmation if needed.
            # ==============================================
            # We need confirmation if any of these are true:
            #
            #   - the other participant is not a stub; we are taking the
            #       account elsewhere away from another viable participant
            #
            #   - we already have an account elsewhere connected from the given
            #       platform, and it will be handed off to a new stub
            #       participant

            other_is_a_real_participant = other.status != 'stub'

            we_already_have_that_kind_of_account = cursor.one("""
                SELECT true
                  FROM elsewhere
                 WHERE participant=%s AND platform=%s
            """, (self.id, platform), default=False)

            need_confirmation = NeedConfirmation(
                other_is_a_real_participant,
                we_already_have_that_kind_of_account,
            )
            if need_confirmation and not have_confirmation:
                raise need_confirmation

            # Move any old account out of the way
            if we_already_have_that_kind_of_account:
                new_stub = Participant.make_stub(cursor)
                cursor.run("""
                    UPDATE elsewhere
                       SET participant=%s
                     WHERE platform=%s
                       AND participant=%s
                """, (new_stub.id, platform, self.id))

            # Do the deal
            cursor.run("""
                UPDATE elsewhere
                   SET participant=%s
                 WHERE platform=%s
                   AND domain=%s
                   AND user_id=%s
            """, (self.id, platform, domain, user_id))

            # Turn pledges into actual tips
            if old_tips:
                x, y = self.id, other.id
                cursor.run(CREATE_TEMP_TABLE_FOR_TIPS, dict(live=x, dead=y))
                cursor.run(CONSOLIDATE_TIPS_RECEIVING, dict(live=x, dead=y))
                cursor.run(ZERO_OUT_OLD_TIPS_RECEIVING, (other.id,))

            # Try to delete the stub account, or prevent new pledges to it
            if not other_is_a_real_participant:
                cursor.run("""
                    DO $$
                    BEGIN
                        DELETE FROM participants WHERE id = %(dead)s;
                    EXCEPTION WHEN OTHERS THEN
                        UPDATE participants
                           SET goal = -1
                         WHERE id = %(dead)s;
                    END;
                    $$ LANGUAGE plpgsql;
                """, dict(dead=other.id))

            # Log the event
            self.add_event(cursor, 'take-over', dict(
                platform=platform, domain=domain, user_id=user_id, owner=other.id
            ))

        if old_tips:
            self.notify_patrons(elsewhere, tips=old_tips)

        self.update_avatar()

        # Note: the order matters here, receiving needs to be updated before giving
        self.update_receiving()
        self.update_giving()

    def delete_elsewhere(self, platform, domain, user_id):
        user_id = str(user_id)
        with self.db.get_cursor() as c:
            c.one("""
                DELETE FROM elsewhere
                 WHERE participant=%s
                   AND platform=%s
                   AND domain=%s
                   AND user_id=%s
             RETURNING participant
            """, (self.id, platform, domain, user_id), default=NonexistingElsewhere)
            detached_repos_count = c.one("""
                WITH detached AS (
                         UPDATE repositories
                            SET participant = null
                          WHERE participant = %s
                            AND platform = %s
                            AND owner_id = %s
                      RETURNING id
                     )
                SELECT count(*) FROM detached
            """, (self.id, platform, user_id))
            self.add_event(c, 'delete_elsewhere', dict(
                platform=platform, domain=domain, user_id=user_id,
                detached_repos_count=detached_repos_count,
            ))
        self.update_avatar()


    # Repositories
    # ============

    def get_repos_for_profile(self):
        return self.db.all("""
            SELECT r
              FROM repositories r
             WHERE r.participant = %s
               AND r.show_on_profile
          ORDER BY r.is_fork ASC NULLS FIRST, r.last_update DESC
             LIMIT 20
        """, (self.id,))

    def get_repos_on_platform(self, platform, limit=50, offset=None):
        return self.db.all("""
            SELECT r
              FROM repositories r
             WHERE r.participant = %s
               AND r.platform = %s
          ORDER BY r.is_fork ASC NULLS FIRST, r.last_update DESC
             LIMIT %s
            OFFSET %s
        """, (self.id, platform, limit, offset))


    # More Random Stuff
    # =================

    def to_dict(self, details=False, inquirer=None):
        output = {
            'id': self.id,
            'username': self.username,
            'avatar': self.avatar_url,
            'kind': self.kind,
        }

        if not details:
            return output

        # Key: npatrons
        output['npatrons'] = self.npatrons

        # Key: goal
        # Values:
        #   undefined - user is not here to receive tips, but will generally regift them
        #   null - user has no funding goal
        #   3.00 - user wishes to receive at least this amount
        if self.goal != 0:
            if self.goal and self.goal > 0:
                goal = str(self.goal)
            else:
                goal = None
            output['goal'] = goal

        # Key: receiving
        # Values:
        #   null - user is receiving anonymously
        #   3.00 - user receives this amount in tips
        if not self.hide_receiving:
            receiving = str(self.receiving)
        else:
            receiving = None
        output['receiving'] = receiving

        # Key: giving
        # Values:
        #   null - user is giving anonymously
        #   3.00 - user gives this amount in tips
        if not self.hide_giving:
            giving = str(self.giving)
        else:
            giving = None
        output['giving'] = giving

        # Key: my_tip
        # Values:
        #   undefined - user is not authenticated
        #   "self" - user == participant
        #   null - user has never tipped this person
        #   0.00 - user used to tip this person but now doesn't
        #   3.00 - user tips this person this amount
        if inquirer:
            if inquirer.id == self.id:
                my_tip = 'self'
            else:
                my_tip = inquirer.get_tip_to(self)['amount']
            output['my_tip'] = str(my_tip)

        # Key: elsewhere
        accounts = self.get_accounts_elsewhere()
        elsewhere = output['elsewhere'] = {}
        for platform, account in accounts.items():
            fields = ['id', 'user_id', 'user_name']
            elsewhere[platform] = {k: getattr(account, k, None) for k in fields}

        return output

    def path(self, path):
        return '/%s/%s' % (self.username, path)

    @property
    def is_person(self):
        return self.kind in ('individual', 'organization')

    def controls(self, other):
        return isinstance(other, Participant) and (
            self.id == other.id or
            other.kind == 'group' and self.member_of(other)
        )

    def update_bit(self, column, bit, on):
        """Updates one bit in an integer in the participants table.

        Bits are used for email notification preferences and privacy settings.
        """
        assert isinstance(getattr(self, column), int)  # anti sql injection
        if on:
            mask = bit
            op = '|'
        else:
            mask = 2147483647 ^ bit
            op = '&'
        r = self.db.one("""
            UPDATE participants
               SET {column} = {column} {op} %s
             WHERE id = %s
         RETURNING {column}
        """.format(column=column, op=op), (mask, self.id))
        self.set_attributes(**{column: r})


class NeedConfirmation(Exception):
    """Represent the case where we need user confirmation during a merge.

    This is used in the workflow for merging one participant into another.

    """

    def __init__(self, a, c):
        self.other_is_a_real_participant = a
        self.we_already_have_that_kind_of_account = c
        self._all = (a, c)

    def __repr__(self):
        return "<NeedConfirmation: %r %r>" % self._all
    __str__ = __repr__

    def __eq__(self, other):
        return self._all == other._all

    def __ne__(self, other):
        return not self.__eq__(other)

    def __bool__(self):
        return any(self._all)
    __nonzero__ = __bool__
