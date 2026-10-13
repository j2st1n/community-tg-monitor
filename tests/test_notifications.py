import unittest
import io
import urllib.error
import urllib.response
from email.message import Message
from types import SimpleNamespace
from unittest.mock import Mock, patch
from app import main


class StopPolling(BaseException):
    pass


class SbsbIdentityRefreshTest(unittest.TestCase):
    @staticmethod
    def homepage(uid):
        return ('<div class="top"><div class="nav-user-links">'
                f'<a href="/u/{uid}/">主页</a><a href="/u/{uid}/notifications/">通知</a>'
                '</div></div><div class="post-content"><a href="/u/999/">正文中的用户</a></div>')

    @staticmethod
    def response(body, status=200, headers=None):
        r = Mock(status=status, headers=headers or {})
        r.read.return_value = body.encode()
        r.__enter__ = Mock(return_value=r)
        r.__exit__ = Mock(return_value=False)
        return r

    @staticmethod
    def bot(uid=None):
        return SimpleNamespace(sbsb_cookie='synthetic-session=value', sbsb_uid=uid,
                               seen_msgs={'existing'}, last_cookie_warn_time=0,
                               admin_chat_id='synthetic-chat', send_msg=Mock(),
                               save_seen_msgs=Mock(), remember_seen_msg=Mock(return_value=False))

    def test_uid_changes_without_restart_and_seen_records_are_preserved(self):
        bot = self.bot()
        requested, homes = [], []
        def request(req, timeout):
            requested.append(req.full_url)
            if req.full_url == 'https://sb.sb/':
                uid = '7' if not homes else '9'
                homes.append(uid)
                return self.response(self.homepage(uid))
            if req.full_url.endswith('/notifications/'):
                return self.response(self.homepage(homes[-1]))
            self.assertEqual(req.full_url, 'https://sb.sb/messages/')
            return self.response('')
        opener = Mock()
        opener.open.side_effect = request
        with patch.object(main.urllib.request, 'build_opener', return_value=opener), \
             patch.object(main.time, 'sleep', side_effect=[None, StopPolling]), \
             patch('builtins.print'):
            with self.assertRaises(StopPolling): main.sbsb_private_messages_thread(bot)
        notifications = [u for u in requested if u.endswith('/notifications/')]
        self.assertEqual(notifications, ['https://sb.sb/u/7/notifications/',
                                         'https://sb.sb/u/9/notifications/'])
        self.assertEqual(homes, ['7', '9'])
        self.assertEqual(bot.sbsb_uid, '9')
        self.assertEqual(bot.seen_msgs, {'existing'})
        bot.send_msg.assert_not_called()

    def test_missing_identity_does_not_reuse_cached_uid_or_visit_notifications(self):
        bot = self.bot('7')
        opener = Mock()
        opener.open.return_value = self.response('<a href="/u/999/">帖子作者</a>')
        with patch.object(main.urllib.request, 'build_opener', return_value=opener), \
             patch.object(main.time, 'sleep', side_effect=StopPolling), patch('builtins.print'):
            with self.assertRaises(StopPolling): main.sbsb_private_messages_thread(bot)
        self.assertIsNone(bot.sbsb_uid)
        self.assertEqual([c.args[0].full_url for c in opener.open.call_args_list], ['https://sb.sb/'])
        bot.send_msg.assert_not_called()
        bot.save_seen_msgs.assert_not_called()

    def test_identity_comes_only_from_navigation_and_requires_unique_uid(self):
        for body in (self.homepage('7'), '<header><a href="/u/7/?tab=notifications">通知</a></header>'):
            opener = Mock()
            opener.open.return_value = self.response(body)
            self.assertEqual(main.fetch_sbsb_current_uid('synthetic-token', opener), '7')
            req = opener.open.call_args.args[0]
            self.assertEqual(req.get_method(), 'GET')
            self.assertEqual(req.full_url, 'https://sb.sb/')
            self.assertEqual(req.get_header('Cookie'), '__Host-bbs_session=synthetic-token')
        for body in ('<a href="/u/999/">作者</a>', self.homepage('0'),
                     self.homepage('7').replace('</div></div>', '<a href="/u/9/">其他身份</a></div></div>')):
            opener.open.return_value = self.response(body)
            with self.assertRaises(main.SbsbIdentityError) as exc:
                main.fetch_sbsb_current_uid('synthetic-token', opener)
            self.assertEqual(exc.exception.code, 'identity_unconfirmed')

    def test_identity_query_failures_are_safe_and_do_not_guess(self):
        for error in (TimeoutError('synthetic-secret'), urllib.error.URLError('synthetic-secret'),
                      RuntimeError('synthetic-secret')):
            opener = Mock()
            opener.open.side_effect = error
            with self.assertRaises(main.SbsbIdentityError) as exc:
                main.fetch_sbsb_current_uid('synthetic-token', opener)
            self.assertEqual(exc.exception.code, 'identity_query_failed')
            self.assertNotIn('synthetic-secret', str(exc.exception))
        opener = Mock()
        with self.assertRaises(main.SbsbIdentityError): main.fetch_sbsb_current_uid('', opener)
        opener.open.assert_not_called()

    def test_identity_rejects_non_success_or_oversized_pages(self):
        for response, expected in ((self.response(self.homepage('7'), status=503), 'identity_query_failed'),
                                   (self.response('x' * 1500001), 'identity_page_too_large')):
            opener = Mock()
            opener.open.return_value = response
            with self.assertRaises(main.SbsbIdentityError) as exc:
                main.fetch_sbsb_current_uid('synthetic-token', opener)
            self.assertEqual(exc.exception.code, expected)

    def test_login_redirect_warns_once_and_skips_notification_fetch(self):
        bot = self.bot('7')
        opener = Mock()
        def redirect(req, **kw):
            raise urllib.error.HTTPError(req.full_url, 302, 'redirect', {'Location': '/login/'}, io.BytesIO())
        opener.open.side_effect = redirect
        with patch.object(main.urllib.request, 'build_opener', return_value=opener), \
             patch.object(main.time, 'sleep', side_effect=[None, StopPolling]), \
             patch.object(main.time, 'time', return_value=100000), patch('builtins.print'):
            with self.assertRaises(StopPolling): main.sbsb_private_messages_thread(bot)
        self.assertIsNone(bot.sbsb_uid)
        self.assertEqual(bot.send_msg.call_count, 1)
        self.assertTrue(all(c.args[0].full_url == 'https://sb.sb/' for c in opener.open.call_args_list))

    def test_redirects_are_never_followed_or_sent_cookie_to_other_origin(self):
        build_opener = main.urllib.request.build_opener
        for status in (301, 302, 303, 307, 308):
            requested = []
            class Redirect(main.urllib.request.BaseHandler):
                handler_order = 100
                def https_open(self, req):
                    requested.append(req.full_url)
                    headers = Message()
                    headers['Location'] = 'https://invalid.example/u/7/'
                    r = urllib.response.addinfourl(io.BytesIO(), headers, req.full_url, status)
                    r.msg = 'redirect'
                    return r
            def fake_opener(*handlers):
                return build_opener(*handlers, Redirect())
            bot = self.bot('7')
            with patch.object(main.urllib.request, 'build_opener', side_effect=fake_opener), \
                 patch.object(main.time, 'sleep', side_effect=StopPolling), patch('builtins.print'):
                with self.assertRaises(StopPolling): main.sbsb_private_messages_thread(bot)
            self.assertEqual(requested, ['https://sb.sb/'])
            self.assertIsNone(bot.sbsb_uid)
            bot.send_msg.assert_not_called()

    def test_notification_identity_change_is_rejected_before_processing(self):
        bot = self.bot('7')
        opener = Mock()
        opener.open.side_effect = [self.response(self.homepage('7')), self.response(self.homepage('9'))]
        with patch.object(main.urllib.request, 'build_opener', return_value=opener), \
             patch.object(main.time, 'sleep', side_effect=StopPolling), \
             patch.object(main, 'parse_sbsb_notifications') as parse, patch('builtins.print'):
            with self.assertRaises(StopPolling): main.sbsb_private_messages_thread(bot)
        self.assertIsNone(bot.sbsb_uid)
        parse.assert_not_called()
        bot.send_msg.assert_not_called()
        self.assertEqual(bot.seen_msgs, {'existing'})


class SbsbNotificationParserTest(unittest.TestCase):
    """测试烧饼论坛 (sb.sb) 互动通知解析、去重机制、时间健壮性与已读识别"""

    def test_time_with_data_abs_attribute(self):
        """1. 验证包含 data-abs='date' 等额外属性的 <time> 标签解析准确性"""
        html = """
        <ul class="notification-list">
          <li class="notification-item">
            <div class="notification-header">
              <a href="/u/1206/">mubdao</a>
              <span class="notification-kind">回复</span>
              <time data-abs="date" datetime="2026-09-10T12:00:00Z" class="time-meta">2026-09-10 12:00</time>
            </div>
            <div class="notification-content">测试包含属性的时间解析</div>
            <a class="notification-reply-action" href="/t/2916/#reply-1001">查看回复</a>
          </li>
        </ul>
        """
        items = main.parse_sbsb_notifications(html)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["iso_time"], "2026-09-10T12:00:00Z")
        self.assertEqual(items[0]["rel_time"], "2026-09-10 12:00")

    def test_time_relative_without_attributes(self):
        """验证无额外属性的相对时间解析"""
        html = """
        <ul class="notification-list">
          <li class="notification-item">
            <div class="notification-header">
              <a href="/u/1206/">mubdao</a>
              <span class="notification-kind">回复</span>
              <time>5分钟前</time>
            </div>
            <div class="notification-content">测试相对时间</div>
            <a class="notification-reply-action" href="/t/2916/#reply-1002">查看回复</a>
          </li>
        </ul>
        """
        items = main.parse_sbsb_notifications(html)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["iso_time"], "")
        self.assertEqual(items[0]["rel_time"], "5分钟前")

    def test_time_missing_forbidden_fallback_to_ganggang(self):
        """验证无 <time> 标签时严禁默认兜底赋值为'刚刚'"""
        html = """
        <ul class="notification-list">
          <li class="notification-item">
            <div class="notification-header">
              <a href="/u/1206/">mubdao</a>
              <span class="notification-kind">系统</span>
            </div>
            <div class="notification-content">缺少时间标签的消息</div>
            <a href="/t/2916/">查看</a>
          </li>
        </ul>
        """
        items = main.parse_sbsb_notifications(html)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["rel_time"], "")
        self.assertNotEqual(items[0]["rel_time"], "刚刚", "时间解析失败时严禁兜底赋值为'刚刚'")

    def test_reply_id_extraction_and_unique_key(self):
        """2. 验证 reply_id 提取与不可变业务主键 notif:reply_{id}"""
        cases = [
            ("/t/2916/#reply-8888", "8888"),
            ("/t/2916/?reply_id=8888", "8888"),
            ("/t/2916/reply-8888", "8888"),
            ("/t/2916/?reply=8888", "8888"),
            ("/t/2916/#8888", "8888"),
            ("/t/2916/#r8888", "8888"),
            ("/reply/8888", "8888"),
        ]
        for link, expected_reply_id in cases:
            html = f"""
            <ul class="notification-list">
              <li class="notification-item">
                <a href="/u/1206/">mubdao</a>
                <span class="notification-kind">回复</span>
                <div class="notification-content">内容</div>
                <a class="notification-reply-action" href="{link}">直达</a>
              </li>
            </ul>
            """
            items = main.parse_sbsb_notifications(html)
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["reply_id"], expected_reply_id)
            self.assertEqual(items[0]["unique_key"], f"notif:reply_{expected_reply_id}")

    def test_data_reply_id_attribute(self):
        """验证从 HTML 属性 data-reply-id 提取 reply_id"""
        html = """
        <ul class="notification-list">
          <li class="notification-item" data-reply-id="6666">
            <a href="/u/1206/">mubdao</a>
            <span class="notification-kind">点赞</span>
            <div class="notification-content">赞了你</div>
            <a href="/t/2916/">查看</a>
          </li>
        </ul>
        """
        items = main.parse_sbsb_notifications(html)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["reply_id"], "6666")
        self.assertEqual(items[0]["unique_key"], "notif:reply_6666")

    def test_hash_key_generation_without_reply_id(self):
        """验证无 reply_id 时生成 stable SHA-256 哈希主键 notif:hash_{digest}"""
        html = """
        <ul class="notification-list">
          <li class="notification-item">
            <a href="/u/1206/">mubdao</a>
            <span class="notification-kind">提及</span>
            <div class="notification-content">提到了你</div>
            <a href="/t/2916/">查看主题</a>
          </li>
        </ul>
        """
        items = main.parse_sbsb_notifications(html)
        self.assertEqual(len(items), 1)
        self.assertIsNone(items[0]["reply_id"])
        self.assertTrue(items[0]["unique_key"].startswith("notif:hash_"))
        self.assertEqual(len(items[0]["unique_key"]), len("notif:hash_") + 64)

    def test_dedup_key_invariance_when_time_changes(self):
        """验证时间发生变更（如刚刚 -> 10分钟前）时，去重 Key 保持绝对不变，杜绝回推"""
        html_t0 = """
        <ul class="notification-list">
          <li class="notification-item">
            <a href="/u/1206/">mubdao</a>
            <span class="notification-kind">回复</span>
            <time datetime="2026-09-10T12:00:00Z">刚刚</time>
            <div class="notification-content">你好世界</div>
            <a class="notification-reply-action" href="/t/2916/#reply-999">回复</a>
          </li>
          <li class="notification-item">
            <a href="/u/42/">tester</a>
            <span class="notification-kind">支持</span>
            <time>刚刚</time>
            <div class="notification-content">给主题加了油</div>
            <a href="/t/2916/">主题</a>
          </li>
        </ul>
        """

        html_t1 = """
        <ul class="notification-list">
          <li class="notification-item">
            <a href="/u/1206/">mubdao</a>
            <span class="notification-kind">回复</span>
            <time datetime="2026-09-10T12:00:00Z">10分钟前</time>
            <div class="notification-content">你好世界</div>
            <a class="notification-reply-action" href="/t/2916/#reply-999">回复</a>
          </li>
          <li class="notification-item">
            <a href="/u/42/">tester</a>
            <span class="notification-kind">支持</span>
            <time>10分钟前</time>
            <div class="notification-content">给主题加了油</div>
            <a href="/t/2916/">主题</a>
          </li>
        </ul>
        """

        items_t0 = main.parse_sbsb_notifications(html_t0)
        items_t1 = main.parse_sbsb_notifications(html_t1)

        self.assertEqual(len(items_t0), 2)
        self.assertEqual(len(items_t1), 2)

        # 包含 reply_id 的条目 key 绝对一致
        self.assertEqual(items_t0[0]["unique_key"], items_t1[0]["unique_key"])
        self.assertEqual(items_t0[0]["unique_key"], "notif:reply_999")

        # 无 reply_id、依赖 SHA-256 哈希的条目 key 绝对一致
        self.assertEqual(items_t0[1]["unique_key"], items_t1[1]["unique_key"])
        self.assertTrue(items_t0[1]["unique_key"].startswith("notif:hash_"))

    def test_username_strips_svg_and_extracts_clean_name(self):
        """3. 验证剥离用户链接内部的 SVG 图标并正确提取用户名"""
        html = """
        <ul class="notification-list">
          <li class="notification-item">
            <a href="/u/1206/">
              <svg class="avatar-icon" viewBox="0 0 24 24"><path d="M12 2L2 7l10 5 10-5-10-5z"/></svg>
              mubdao
            </a>
            <span class="notification-kind">回复</span>
            <div class="notification-content">内容</div>
          </li>
        </ul>
        """
        items = main.parse_sbsb_notifications(html)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["user"], "mubdao")

    def test_username_does_not_mistake_post_title(self):
        """验证提取用户名时杜绝误用 post-title"""
        html = """
        <ul class="notification-list">
          <li class="notification-item">
            <div class="notification-header">
              <a href="/u/42/"><svg><circle/></svg> tester</a> 在主题
              <a class="post-title" href="/t/2916/">吃饼了，饼友们！</a> 中提到了你
            </div>
            <span class="notification-kind">提及</span>
            <div class="notification-content">@you 欢迎</div>
          </li>
        </ul>
        """
        items = main.parse_sbsb_notifications(html)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["user"], "tester")
        self.assertNotEqual(items[0]["user"], "吃饼了，饼友们！", "严禁将帖子标题识别为用户名")

    def test_exact_li_closing_parsing(self):
        """4. 验证列表项通过精确闭合的 </li> 解析，不吞并后续内容"""
        html = """
        <div class="wrapper">
          <ul class="notification-list">
            <li class="notification-item">
              <a href="/u/1/">admin</a>
              <span class="notification-kind">提醒</span>
              <div class="notification-content">通知1</div>
            </li>
            <li class="notification-item">
              <a href="/u/2/">user2</a>
              <span class="notification-kind">回复</span>
              <div class="notification-content">通知2</div>
            </li>
          </ul>
          <div class="footer">这是页脚内容，不应被包含进任何通知项</div>
        </div>
        """
        items = main.parse_sbsb_notifications(html)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["user"], "admin")
        self.assertEqual(items[1]["user"], "user2")
        self.assertNotIn("页脚", items[0]["content"])
        self.assertNotIn("页脚", items[1]["content"])

    def test_read_divider_identification(self):
        """验证支持识别 .notification-read-divider 分割线并标定已读状态"""
        html = """
        <ul class="notification-list">
          <li class="notification-item">
            <a href="/u/101/">new_user</a>
            <span class="notification-kind">回复</span>
            <div class="notification-content">未读新回复</div>
          </li>
          <li class="notification-read-divider">以下为历史已读通知</li>
          <li class="notification-item">
            <a href="/u/102/">old_user</a>
            <span class="notification-kind">点赞</span>
            <div class="notification-content">历史已读点赞</div>
          </li>
          <li class="notification-item">
            <a href="/u/103/">old_user2</a>
            <span class="notification-kind">支持</span>
            <div class="notification-content">历史已读支持</div>
          </li>
        </ul>
        """
        items = main.parse_sbsb_notifications(html)
        self.assertEqual(len(items), 3)

        # 分割线前为未读
        self.assertFalse(items[0]["is_read"])
        self.assertEqual(items[0]["user"], "new_user")

        # 分割线后均为已读
        self.assertTrue(items[1]["is_read"])
        self.assertEqual(items[1]["user"], "old_user")
        self.assertTrue(items[2]["is_read"])
        self.assertEqual(items[2]["user"], "old_user2")

    def test_empty_and_malformed_html(self):
        """验证空 HTML 与非通知 HTML 的鲁棒性"""
        self.assertEqual(main.parse_sbsb_notifications(""), [])
        self.assertEqual(main.parse_sbsb_notifications("<div>普通页面无通知</div>"), [])


if __name__ == "__main__":
    unittest.main()
