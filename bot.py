import os
import re
import time
import asyncio
import logging
from datetime import datetime
import io
import threading
import json
import zlib
import base64
import contextvars
from urllib.parse import urlencode

import gspread
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from telegram import (BotCommand, BotCommandScopeChat, InlineKeyboardButton,
                      InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, Update, WebAppInfo)
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    TypeHandler,
    filters,
)

REAL_UID = contextvars.ContextVar("real_uid", default=0)      # رقم اللي بعت الرسالة فعلًا (حتى لو المدير شغال كمستخدم تاني)
logging.basicConfig(level=logging.INFO)
load_dotenv()

TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])
SHEET_ID = os.environ["SHEET_ID"]
SA_FILE = os.environ.get("SERVICE_ACCOUNT_FILE", "service_account.json")
# رابط صفحة الفورم (Mini App) على GitHub Pages مثلًا. لو فاضي البوت بيشتغل بالفورم القديمة (الدواير).
MINIAPP_URL = os.environ.get("MINIAPP_URL", "").strip()

creds = Credentials.from_service_account_file(
    SA_FILE, scopes=["https://www.googleapis.com/auth/spreadsheets"]
)
sheet = gspread.authorize(creds).open_by_key(SHEET_ID)

# Conversation states
(SELLER, SEARCH, PICK_CLIENT, PTYPE, AMOUNT, KIND, METHOD,
 OTHER_BANK, DATE, NOTES, CONFIRM, PROJECT, ACCOUNTANT) = range(13)
ATTACH, P_VOUCH = 13, 14          # مرفق التحويل + رقم الوصل (فلو البياع)
S_ATT, R_ATT, C_ATT = 45, 46, 56      # مرفق (اختياري) في كل العمليات
S_VOUCH = 39                      # رقم الوصل (فلو المحاسب)
C_BAYAN, C_AMOUNT, C_DATE, C_PARTY, C_VOUCH, C_CONFIRM = range(50, 56)   # فلو أمين الخزنة
# حالات بوت الموردين
(S_SUP, S_TYPE, S_AMOUNT, S_KIND, S_METHOD, S_OTHER, S_DATE, S_CONFIRM, S_CTYPE, S_EMP,
 S_EL1, S_EL2, S_EL3, S_NOTE, S_SELLER, S_CLIENT, S_PROJECT, S_SALETYPE, S_STL) = range(20, 39)

# أسماء المحاسبين اللي بتتكتب في عمود "المستخدم" (عدّل الإملاء هنا لو محتاج)
ACCOUNTANTS = ["Asem Hosam", "Mahmoud Hadidy"]

# محفظة مستر جمعة: بتظهر في اختيارات المحافظ (مرة واحدة)، وقيدها بيتسجل على العهد
# المديرين: كل واحد في سطر لوحده في قايمة البياعين (الأسماء زي ما هي في تاب Lists)
SOLO_SELLERS = ["م/ احمد رافت", "م/ محمد جمعه"]
# اللقب بيظهر جنب الاسم في زرار القايمة بس، ومش بيتسجل في الطلب ولا القيود
SELLER_TITLES = {"م/ احمد رافت": "CEO", "م/ محمد جمعه": "SM"}

# المحاسبين المسموح لهم بالموردين والعهد والسلف (غير الدفعات). الأونر دايمًا منهم.
# لو في محاسب تاني حط في ملف .env سطر: ADMIN_USERS=123456789=Mahmoud Hadidy;987654321=اسم تاني
ADMIN_USERS = {OWNER_ID: ACCOUNTANTS[0]}
for _p in os.environ.get("ADMIN_USERS", "").split(";"):
    if "=" in _p:
        _k, _v = _p.split("=", 1)
        if _k.strip().isdigit():
            ADMIN_USERS[int(_k)] = _v.strip()
ADMINS = set(ADMIN_USERS)

# المدير التنفيذي: حط رقمه في ملف .env كده: CEO_ID=123456789   (الرقم بيطلع من أمر /myid)
CEO_NAME = "م/ أحمد رأفت"
CEO_IDS = {int(x) for x in os.environ.get("CEO_ID", "").replace(" ", "").split(",") if x.isdigit()}

# ---- الموردين ----
# قايمة الموردين من شيت LVL3 (أول 8 هما الأساسيين)
SUPPLIERS = ["البركة بلس", "ناصر فيجين جلاس", "م . صابر حسن شيش", "الامين ايما بن", "شركة foxywin", "رجب ارجات", "شرفه SHORFA", "المصطفى جلاس", "مكتب دواويين للإستشارات المالية", "شركة فودافون مصر", "احمد عيد لابيكس", "احمد مرسي كومبن ( الزيتون )", "احمد نحله بريمير", "أكابن مورد أصل", "الاتحاد", "الألومنيوم محمد ناصر", "الحلبي", "الشرقاوي", "الصفوة- ايمابين", "إيمابن", "ايمن ارجات", "سكاي", "ليمينيتيد", "محمد السيد الحداد", "محمود مزايا", "مدار شيش", "مصطفى (بليسيه)", "هيثم-كرافت", "وليد زجاج", "وين تاج-كومبن", "وينتك المريوطية", "supreme مورد ديكونيك", "شركة الامل", "مورد نقدي"]
SUPPLIER_TYPES = ["الدفعات", "خصومات", "قيمة المشتريات", "مرتجعات المشتريات", "رصيد إفتتاحى"]
# حساب المورد في اليومية (من القيود اللي بعتها): المستوى الأول والتاني، والمستوى التالت هو اسم المورد
SUP_L1 = "الالتزامات_المتداوله"
SUP_L2 = "الموردين"
# الأنواع اللي ليها قيد متحدد. أي نوع تاني البوت بيرفض يسجله ويقول ارجع لمسؤول الحسابات.
SUP_DONE = {"الدفعات", "خصومات"}
# ---- شراء الأصول (من شجرة الحسابات: LVL1_2 و LVL3 و LVL4) ----
# المدين: الاصول_طويله_الاجل > الاصول_الثابته > نوع الأصل > (الأصل). الدائن: الخزينة/البنك/المحفظة أو المورد (آجل).
ASSET_L1, ASSET_L2 = "الاصول_طويله_الاجل", "الاصول_الثابته"
ASSET_CLASSES = ["اثاث_ومكاتب", "أنظمة_الأمان_والمراقبة", "اجهزه_كمبيوتر_وملحقاتها", "مباني_وعقارات", "اجهزه_كهربائية_وتكييف", "وسائل_اتصالات", "تجهيزات_وديكورات", "الأراضى", "السيارات_والشاحنات", "الأدوات_ومعدات", "الآلات_والماكينات"]
ASSET_ITEMS = {"اثاث_ومكاتب": ["كرسى مكتب", "دولاب", "ترابيزة اجتماعات", "سجاد", "خزينة كاشير", "مكتب"], "أنظمة_الأمان_والمراقبة": ["كاميرات مراقبه", "طفايات حريق", "جهاز DVR"], "اجهزه_كمبيوتر_وملحقاتها": ["هارد ديسك خارجي", "طابعات", "جهاز ديسك توب", "جهاز لاب توب", "داتا شو", "ماكينة تصوير"], "اجهزه_كهربائية_وتكييف": ["مبرد مياه", "مروحة ستاند", "فلتر7 مراحل", "مروحة سقف", "ثلاجة", "شاشة عرض", "ريسيفر", "سخان مياه", "تكييف", "ستائر كهربائية", "ماكينة البصمة", "أجهزة باركود", "ميزان كهربائى", "مروحة حائط", "ميكرويف", "مكواة كهربائية", "أنابيب غاز", "كمبروسر هواء", "تليفيزيون", "ليدات تصوير", "معدات تصوير", "كاميرات تصوير", "ماكمينة قهوه"], "وسائل_اتصالات": ["جهاز تليفون ارضي", "جهاز تليفون محمول", "جهاز تابلت"], "تجهيزات_وديكورات": ["تجهيزات المخزن الرئيسى"]}
CREDIT_OPT = ("credit", "آجل (على مورد)", "🧾 آجل (على مورد)")
NO_MINI = {"buy", "exp", "inc", "gen", "tsearch", "tcount"}      # دي بتفتح جوه تليجرام حتى في وضع رسائل فورم (عشان كل مستويات الشجرة)
NATIVE_ONLY = {"gen", "tsearch", "tcount"}
# ---- العهد والسلف (من القيود الصح في اليومية) ----
CUS_L1, CUS_L2 = "الاصول_المتداوله", "مدينون_وارصده_مدينة"
CUSTODY_TYPES = ["العهد_المؤقتة", "العهد_المستديمة"]          # المستوى التالت
CUSTODY_LABELS = {"العهد_المؤقتة": "عهدة مؤقتة", "العهد_المستديمة": "عهدة مستديمة"}
CUS_DONE = {"العهد_المؤقتة", "العهد_المستديمة"}      # النوع اللي ليه قيد متحدد. المستديمة لسه ماجاش قيدها
ADV_L3 = "سلف_العاملين"
# قايمة الموظفين من شيت Conf. (بنفس المسافات الأصلية عشان تطابق اليومية)
EMPLOYEES = [" احمد عوض انور معروف", " بهاء فارس ابراهيم حسن", " جمعه عيد جمعه احمد", " جهاد طارق فاروق محمد", " حازم سلامه موسى على ", " شريف مصطفي محمد عبدالرحيم ", " عبدالرحمن اشرف أبوالقاسم ياسين", " عبدالرحمن محمد سيد عثمان", " عبدالله أسامة عبدالله محمد الشاهد ", " عمر عادل سلطان أبوالقاسم", " عمرو جمال سليمان عبدالغنى", " محمد ايهاب محرم عبدالحميد", " محمد حسن محمد عبدالله عفيفي ", " محمد حسن ياسين حسن", " محمد خير عبدالمجيد غنام", " محمود سمير ابوالاسعاد السيد فرج", " محمود مراد محمد محمد احمد الروبى", " مصطفى سيد محمد محمد مصطفى", " مصطفى محمد عبداللطيف امين طوطح", " هبة الله رأفت", " هشام ابراهيم عبدالجواد عبدالرازق", " ياسمين عبدالحافظ", " يحى عبدالجواد هلال عبدالجواد غريب ", "احمد اسامه حسن مجرب محمد", "احمد رأفت حلمى عكاشة احمد", "احمد سعد محمود طه", "احمد عبدالله السيد مبروك", "أحمد عطيه ميزار على", "احمد محمد جمال الدين عبدالله", "احمد محمد سعيد عبدالفتاح سمك", "اسلام محمد ابوبكر حسن", "اسلام محمد جمال الدين عبدالله", "اسلام محمود فرحات محمد", "اسلم مجدى عوض ابراهيم ", "اشواق مشهور", "ايهاب حامد محمد حامد صالح", "باسم فتحي كامل علي ", "برنس عبدالصادق سيد محمد", "بسام محمد رفاعى محمد", "بكرى رمضان بكرى محمد", "جبريل شعبان جبريل عبادى", "خميس فراج خميس سلامه", "سالمان نجيب محمد على عبدالعال", "شعبان حسن بيومى محمد ابراهيم ", "عاطف فوزى حمد عبدالرحمن ", "عبدالحميد خالد عبدالله ", "عبدالرحمن احمد حامد عبده سالم", "عبدالرحمن عمر", "عبدالرحمن فوزى خليل محمد خليل", "عبدالرحمن محمد مخزن", "عبدالله فرج محمود جمعه خميس", "على عماد على راشد محمد", "على فوزى احمد عبدالرحمن ", "عمر مصطفى اصلان يوسف", "محسن فراس عبدالحى", "محمد ابراهيم السيد بسيوني", "محمد ابراهيم السيد حسين", "محمد احمد محمد على ابراهيم", "محمد اشرف تركيبات", "محمد ايمن طه ابراهيم طه مصطفي", "محمد جمعه عبدربه عبدالرحمن", "محمد حربى عويس عبدالتواب ", "محمد رأفت حلمى عكاشة", "محمد صالح", "محمد عبدالرحمن المشرقي", "محمد عبدالمقصود عبدالعاطى عبدالمقصود", "محمد على محمد جمعه السيد", "محمد فرج محمود جمعه", "محمد كيلانى دريس عامر عزيز ", "محمد مصطفى مفتاح أحمد", "محمد مصطفي محمد امين", "محمود هانى احمد حسن", "مصطفى عبدالبصير على العزب", "مصطفى محمد عبدالعزيز محمد", "موسى حسن سعيد موسى فضل", "هاني عبدالتواب", "ياسر احمد صابر عبدالعظيم طه", "يحيي عاطف احمد برزى عبدالعال", "يوسف صبحى عبدالسميع عبدالحميد", "يوسف عادل جلال محمد", "هانى المشرقي", "محمد سالم", "اسلام المشرقي", "محمد ايمن المشرقي", "جمال بهي الدين", "عبد الرحمن محمد جمعة", "صلاح الزمر", "عاصم حسام طه "]

# ---- المصروفات (من شيتات LVL1_2 و LVL3) ----
# ---- شجرة الحسابات الكاملة (من شيتات LVL1_2 و LVL3 و LVL4 في ملف اليومية - آخر نسخة) ----
TREE_L1 = ["الارصدة الافتتاحية", "الاصول_طويله_الاجل", "الاصول_المتداوله", "حقوق_الملكيه", "الالتزامات_طويلة_الأجل", "الالتزامات_المتداوله", "ايرادات_النشاط", "م_عموميه_واداريه", "م_البيع_والتوزيع", "تكاليف_النشاط", "م_تسويقيه", "الصدقات_والتبرعات", "إيرادات_أخرى", "م_تمويلية", "ضريبة الدخل"]
TREE_L2 = {
    "الاصول_طويله_الاجل": ["الاصول_الثابته", "مجمع_اهلاك_الاصول_الثابته", "الاصول_غير_الملموسة", "مشروعات_تحت_التنفيذ", "أصول_أخرى", "إستثمارات_فى_شركات_شقيقة", "ودائع_طويلة_الأجل"],
    "الاصول_المتداوله": ["العملاء", "النقدية_بالخزينة", "النقدية_بالبنوك", "أوراق_القبض", "شيكات_تحت_التحصيل", "المخزون", "مدينون_وارصده_مدينة", "المصروفات_المدفوعة_مقدما", "جاري_شركات_شقيقة", "مخزون بضاعة الأمانة"],
    "حقوق_الملكيه": ["رأس_المال", "جاري_الشركاء", "الأرباح_المحتجزة", "أرباح_الفترة", "الاحتياطيات"],
    "الالتزامات_طويلة_الأجل": ["اوراق_الدفع_طويله_الاجل", "قروض_طويله_الاجل", "المخصصات", "حسابات_المستثمرين"],
    "الالتزامات_المتداوله": ["الموردين", "أوراق_الدفع", "دائنون_وارصده_دائنة", "المصروفات_المستحقة", "قروض_قصيرة_الأجل", "عملاء_دفعات_مقدمة", "الإيرادات_غير_المحققة"],
    "ايرادات_النشاط": ["المبيعات", "المردودات", "خصم_مسموح_به"],
    "م_عموميه_واداريه": ["الاجور_والمرتبات", "اتعاب_مهنية", "ادوات مكتبية", "اكراميات", "أمن وحراسة", "انتقالات", "ايجار", "إستهلاك_الغاز", "إستهلاك_الكهرباء", "إستهلاك_المياه", "برامج_ومستلزمات_كمبيوتر", "تليفونات_وانترنت", "سفر ورحلات", "رسوم_وغرامات", "شحن وبريد", "صيانة_عامه", "ضيافه_واستقبال", "م_التأمينات_الاجتماعية", "م_التأمينات_الصحية", "م_بنكية", "م_السيارات", "م_بوفيه_ونظافة", "م_نثرية", "اهلاك_الاصوال_الثابته", "مشال_وتحميل", "بريد ومراسلات", "دورات_تدريبية", "مطبوعات", "ديون_معدومة", "الصدقات_والتبرعات", "م._ضرائب", "م.ترفيهية", "رعاية_طبية"],
    "م_البيع_والتوزيع": ["الاجور_والمرتبات", "ادوات مكتبية", "اكراميات", "أمن وحراسة", "انتقالات", "ايجار", "برامج_ومستلزمات_كمبيوتر", "تليفونات_وانترنت", "صيانة_عامه", "ضيافه_واستقبال", "م_التأمينات_الاجتماعية", "م_التأمينات_الصحية", "م_بوفيه_ونظافة", "م_السيارات", "م_نثرية", "نقل_وشحن", "إستهلاك_الغاز", "إستهلاك_الكهرباء", "إستهلاك_المياه", "دورات_تدريبية", "م. توالف", "مشال_وتحميل", "اهلاك_الاصوال_الثابته", "مطبوعات", "م. الشحن والنقل", "خصم مسموح به"],
    "تكاليف_النشاط": ["تكاليف_نشاط_ثابتة", "تكاليف_نشاط_متغيرة", "تكلفة_البضاعة_المباعة"],
    "م_تسويقيه": ["الاجور_والمرتبات", "دعاية_اعلان", "ندوات_ومعارض", "هدايا"],
    "الصدقات_والتبرعات": ["صدقات", "تبرعات"],
    "إيرادات_أخرى": ["فروق خزينة", "فوائد بنكية", "أرباح بيع خردة", "أرباح وخسائر رأسمالية", "أرباح وخسائر فروق العملة", "أرباح وخسائر بيع أصول ثابتة", "أرباح وخسائر إعادة تقييم الأصول الثابتة", "أرباح وخسائر إعادة تقييم المخزون", "أيرادات مقابل عمليات النقل", "خصم مكتسب", "مصروفات مسترده"],
}
# أبناء أي حساب (مستوى2 أو مستوى3...) بالاسم: بيتدور عليها بعد توحيد الهمزات والمسافات والشرطات
TREE_KIDS = {
    "الاصول_الثابته": ["اثاث_ومكاتب", "أنظمة_الأمان_والمراقبة", "اجهزه_كمبيوتر_وملحقاتها", "مباني_وعقارات", "اجهزه_كهربائية_وتكييف", "وسائل_اتصالات", "تجهيزات_وديكورات", "الأراضى", "السيارات_والشاحنات", "الأدوات_ومعدات", "الآلات_والماكينات"],
    "مشروعات_تحت_التنفيذ": ["مشروع سيستم Odoo"],
    "رأس_المال": ["رأس مال الشريك احمد رافت", "رأس مال الشريك محمد جمعه"],
    "جاري_الشركاء": ["جارى الشريك  احمد رافت", "جارى الشريك  محمد جمعه"],
    "الأرباح_المحتجزة": ["أرباح أعوام سابقة", "أرباح_العام السابق"],
    "اهلاك_الاصوال_الثابته": ["إهلاك_اثاث_ومكاتب", "إهلاك_أنظمة_الأمان_والمراقبة", "إهلاك_اجهزه_كمبيوتر_وملحقاتها", "إهلاك_مبانى_وعقارات", "إهلاك_اجهزه_كهربائية_وتكييف", "إهلاك_وسائل_اتصالات", "إهلاك_تجهيزات_وديكورات", "إهلاك_السيارات_والشاحنات", "إهلاك_الأدوات_ومعدات", "إهلاك_الآلات_والماكينات"],
    "مجمع_اهلاك_الاصول_الثابته": ["مجمع_إهلاك_اثاث_ومكاتب", "مجمع_إهلاك_أنظمة_الأمان_والمراقبة", "مجمع_إهلاك_اجهزه_كمبيوتر_وملحقاتها", "مجمع_إهلاك_مبانى_وعقارات", "مجمع_إهلاك_اجهزه_كهربائية_وتكييف", "مجمع_إهلاك_وسائل_اتصالات", "مجمع_إهلاك_تجهيزات_وديكورات", "مجمع_إهلاك_السيارات_والشاحنات", "مجمع_إهلاك_الأدوات_ومعدات", "مجمع_إهلاك_الآلات_والماكينات"],
    "النقدية_بالخزينة": ["الخزينة_الرئيسية", "خزينة المناديب", "الخزينه الفرعيه", "خزينة الفروع", "خزينة_الإيداعات_الوسيطة", "الخزينة_الرئيسية_دولار_أمريكى", "الخزينة_الرئيسية_ريال_سعودى"],
    "النقدية_بالبنوك": ["بنك_البركة_شركة", "بنك_QNB شخصى أحمد رأفت", "بنك_CIB شخصى أحمد رأفت", "بنك_QNB دولار أمريكى شخصى أحمد رأفت", "حساب_فودافون_كاش", "حساب_فودافون_كاش ( المحفظة الاحتياطية )", "البنك_الأهلى_المصرى", "بنك_مصر", "بنك CIB دولار أمريكى", "البريد_المصري", "حساب مصاري", "إيداعات_تحت_التحصيل"],
    "مدينون_وارصده_مدينة": ["سلف_العاملين", "العهد_المستديمة", "العهد_المؤقتة", "ضرائب_خصم_المنبع", "مدينون_متنوعون", "التأمينات_لدى_الغير"],
    "المصروفات_المدفوعة_مقدما": ["التأمينات الصحية", "إيجار المقر", "إشتراك سيستم الحسابات"],
    "المخزون": ["مخزن المواد الخام", "مخزن تحت التشغيل", "مخزن منتج نهائى", "بضاعة بالطريق"],
    "الموردين": ["مكتب دواويين للإستشارات المالية", "شركة فودافون مصر", "احمد عيد لابيكس", "احمد مرسي كومبن ( الزيتون )", "احمد نحله بريمير", "أكابن مورد أصل", "الاتحاد", "الألومنيوم محمد ناصر", "الامين ايما بن", "البركة بلس", "الحلبي", "الشرقاوي", "الصفوة- ايمابين", "المصطفى جلاس", "إيمابن", "ايمن ارجات", "رجب ارجات", "سكاي", "شرفه SHORFA", "ليمينيتيد", "م . صابر حسن شيش", "محمد السيد الحداد", "محمود مزايا", "مدار شيش", "مصطفى (بليسيه)", "ناصر فيجين جلاس", "هيثم-كرافت", "وليد زجاج", "وين تاج-كومبن", "وينتك المريوطية", "supreme مورد ديكونيك", "شركة الامل", "مورد نقدي", "شركة foxywin"],
    "دائنون_وارصده_دائنة": ["دائنى_الأصول_الثابتة", "المدفوع_تحت_حساب_الزكاة", "هيئة_التأمينات_الإجتماعية", "ضريبة_القيمة_المضافة", "مأمورية_ضرائب_الخصم_والتحصيل", "مصلحة_الضرائب_العامة", "مأمورية_ضرائب_كسب_العمل", "تأمينات_من_الغير", "صندوق مكافأت العاملين"],
    "المصروفات_المستحقة": ["العمولات_المستحقة", "المرتبات_المستحقة", "إيجارت مستحقة", "الحوافز_المستحقة"],
    "الاجور_والمرتبات": ["أجر_أساسى", "عمولات_البيع", "عمولات_التحصيل", "مكافات", "بدل_إنتقالات", "بدل_سفر", "بدل_وجبة", "بدل زي و ملبوسات", "يوميات عمال", "أجر_إضافى"],
    "م_السيارات": ["وقود", "زيوت", "قطع_غيار", "صيانة", "م_تراخيص", "رسوم_طريق_كارتات", "ايجار", "جراج"],
    "تليفونات_وإنترنت": ["أرضى", "محمول", "أنترنت_ADSL", "أنترنت_USB_Modem", "Hosting_&_Domain", "شركة SMS Misr"],
    "نقل_وشحن": ["وقود", "زيوت", "قطع_غيار", "صيانة", "م_تراخيص", "رسوم_طريق_كارتات", "ايجار_خارجي", "اهلاك_السيارة", "مرتب السائق"],
    "م_بنكية": ["مصاريف كشف حساب", "اصدار دفاتر شيكات", "عمولة تحصيل شيكات", "مصاريف ارتداد شيك", "فوائد مدينة", "م.الصيانة الشهرية", "عمولة الحد الأدنى للحساب", "عمولات خطابات الضمان", "عمولة.سداد.المرتبات", "عمولات تحويل", "عمولة.التحصيل.بالفيزا", "عمولة.التحصيل.بالمحفظة.الإلكترونية", "مصاريف فح حساب"],
    "اتعاب_مهنية": ["محاسب_قانونى", "محاماه", "Freelancing", "توظيف"],
    "دعاية_اعلان": ["Facebook", "TikTok", "Linked in", "What's App", "مطبوعات"],
    "م.تعبئة_وتغليف": ["فلايرات"],
    "تكاليف_نشاط_ثابتة": ["الاجور_والمرتبات", "تليفونات_وانترنت", "ايجار", "ادوات مكتبية", "صيانة_عامه", "إستهلاك_الكهرباء", "إستهلاك_الغاز", "إستهلاك_المياه", "م_بوفيه_ونظافة", "م_نثرية", "أمن وحراسة", "برامج_ومستلزمات_كمبيوتر", "اهلاك_الاصوال_الثابته"],
    "تكاليف_نشاط_متغيرة": ["نقل_وشحن", "م_السيارات", "م.تعبئة_وتغليف", "انتقالات", "مطبوعات", "اكراميات", "مصاريف المصنع", "تكلفة زجاج", "تكلفة بيلسيه"],
    "تكلفة_البضاعة_المباعة": ["تكلفة المبيعات", "تكلفة مردودات المبيعات", "الخصم المكتسب"],
    "م. ضرائب": ["م. ضرائب كسب العمل", "م. ضرائب فواتير موردين", "م. ضرائب خصم المنبع"],
    "اثاث_ومكاتب": ["كرسى مكتب", "دولاب", "ترابيزة اجتماعات", "سجاد", "خزينة كاشير", "مكتب"],
    "أنظمة_الأمان_والمراقبة": ["كاميرات مراقبه", "طفايات حريق", "جهاز DVR"],
    "اجهزه_كمبيوتر_وملحقاتها": ["هارد ديسك خارجي", "طابعات", "جهاز ديسك توب", "جهاز لاب توب", "داتا شو", "ماكينة تصوير"],
    "اجهزه_كهربائية_وتكييف": ["مبرد مياه", "مروحة ستاند", "فلتر7 مراحل", "مروحة سقف", "ثلاجة", "شاشة عرض", "ريسيفر", "سخان مياه", "تكييف", "ستائر كهربائية", "ماكينة البصمة", "أجهزة باركود", "ميزان كهربائى", "مروحة حائط", "ميكرويف", "مكواة كهربائية", "أنابيب غاز", "كمبروسر هواء", "تليفيزيون", "ليدات تصوير", "معدات تصوير", "كاميرات تصوير", "ماكمينة قهوه"],
    "وسائل_اتصالات": ["جهاز تليفون ارضي", "جهاز تليفون محمول", "جهاز تابلت"],
    "تجهيزات_وديكورات": ["تجهيزات المخزن الرئيسى"],
    "التأمينات_لدى_الغير": ["تأمين إيجار المقر الإدارى بأكتوبر", "تأمين إيجار مقر المصنع بالهرم"],
    "الصدقات_والتبرعات": ["صدقات", "تبرعات"],
}
EXP_L1_LIST = ["م_عموميه_واداريه", "م_البيع_والتوزيع", "تكاليف_النشاط", "م_تسويقيه", "الصدقات_والتبرعات"]
_NK_T = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ة": "ه", "ى": "ي"})


def nk(s):
    return re.sub(r"[\s_.]+", "", str(s).translate(_NK_T)).lower()


_KIDS_N = {nk(k): v for k, v in TREE_KIDS.items()}


def tree_kids(path):
    """أبناء العقدة اللي المسار بتاعها path (قايمة: مستوى1 ثم مستوى2 ...). فاضية لو مفيش."""
    if not path:
        return list(TREE_L1)
    if len(path) == 1:
        return list(TREE_L2.get(path[0], []))
    if len(path) >= 4:
        return []
    return list(_KIDS_N.get(nk(path[-1]), []))


EXP_TREE = {k: list(TREE_L2[k]) for k in EXP_L1_LIST}
EXP_L3 = {l2: tree_kids(["x", l2]) for l1 in EXP_TREE for l2 in EXP_TREE[l1] if tree_kids(["x", l2])}
EXP_L1 = list(EXP_TREE)
# حسابات الإيرادات: بتتقرا من تاب Income_Accounts في الشيت (الأعمدة: المستوى1 | المستوى2 | المستوى3).
INCOME_TAB = "Income_Accounts"
_INC = {"t": 0.0, "rows": []}


def income_accounts():
    """بترجّع [(مستوى1, مستوى2, مستوى3)]. بتتخزن دقيقة عشان ما نقراش الشيت كل ضغطة."""
    if time.time() - _INC["t"] < 60 and _INC["rows"]:
        return _INC["rows"]
    ws, new = _tab(INCOME_TAB, 500, 3)
    if new:
        ws.update("A1", [["المستوى1", "المستوى2", "المستوى3"]])
    rows = []
    for r in ws.get_all_values()[1:]:
        r = (r + ["", "", ""])[:3]
        if r[0].strip() and r[1].strip():
            rows.append((r[0].strip(), r[1].strip(), r[2].strip()))
    if not rows:      # التاب فاضي: ناخد إيرادات_أخرى من شجرة الحسابات
        rows = [("إيرادات_أخرى", x, "") for x in TREE_L2.get("إيرادات_أخرى", [])]
    _INC.update(t=time.time(), rows=rows)
    return rows


def income_tree():
    tree, l3m = {}, {}
    for a, b, c in _INC["rows"]:
        tree.setdefault(a, [])
        if b not in tree[a]:
            tree[a].append(b)
        if c:
            l3m.setdefault(b, [])
            if c not in l3m[b]:
                l3m[b].append(c)
    return tree, l3m


def preset_next(d):
    """التحويل المعتمد: الحساب والمبلغ والتاريخ جاهزين، فنروح لرقم الوصل (لو خزينة) أو للتأكيد."""
    return S_VOUCH if d.get("kind") == "cash" and not d.get("voucher") else S_CONFIRM


def tree_of(d):
    if d["flow"] == "inc":
        tree, l3m = income_tree()
        return list(tree), tree, l3m
    return EXP_L1, EXP_TREE, EXP_L3

SALE_TYPES = ["قيمة المبيعات", "مرتجعات المبيعات", "خصومات", "رصيد إفتتاحى"]   # من قايمة العملاء في Conf.

# الحساب المقابل لقيد الخصم (من القيد اللي بعتّه)
DISC_L1, DISC_L2 = "إيرادات_أخرى", "خصم مكتسب"

# محفظة مستر جمعة وقيد العهدة بتاعها
GOMAA_WALLET = "محفظة مستر جمعة"
CUSTODY_PERSON = "محمد جمعه عبدربه عبدالرحمن"
CUSTODY_L2 = "مدينون_وارصده_مدينة"
CUSTODY_L3 = "العهد_المؤقتة"

# أرقام المحافظ اللي بتظهر تحت اسم المحفظة في الاختيار: {"اسم المحفظة زي ما بتظهر": "الرقم"}
WALLET_NUMBERS = {
    "فودافون كاش": "01020401577",
    "م/ محمد جمعة": "01008557655",
    # "فودافون كاش المحفظة الاحتياطية": "الرقم هنا",
}

AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩٫٬", "0123456789.,")
_PCT_RE = re.compile(r"\s*\d+(?:[.,]\d+)?\s*[%٪]")


def nopct(s):
    """للعرض بس: بيشيل النسبة من اسم الدفعة (القيود بتفضل بالاسم الأصلي)."""
    return re.sub(r"\s{2,}", " ", _PCT_RE.sub("", str(s))).strip()

NORM = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ة": "ه", "ى": "ي"})


def norm(s):
    return s.translate(NORM).strip().lower()


def is_wallet(name):
    n = norm(name)
    return "محفظ" in n or "فودافون" in n or "كاش" in n


def wallet_label(name):
    """الاسم اللي بيظهر في زرار المحفظة للبياع بس. القيم الأصلية والقيود ما بتتغيرش."""
    n = norm(name)
    if "جمعه" in n:
        return "م/ محمد جمعة"
    if "احتياط" in n:
        return "فودافون كاش المحفظة الاحتياطية"
    return "فودافون كاش"


async def run(fn, *a, **kw):
    return await asyncio.to_thread(fn, *a, **kw)



# ---------------- كتابة القيود في GL_Entries (مع تحقق إنها اتكتبت فعلًا) ----------------
GL_TAB = "GL_Entries"
# الشيت التاني (اختياري): حط في .env السطرين دول لما تبعتلي اللينك
#   GL2_SHEET_ID=...   و   GL2_TAB=اسم التاب
GL2_SHEET_ID = os.environ.get("GL2_SHEET_ID", "").strip()
GL2_TAB = os.environ.get("GL2_TAB", "GL").strip()
CASH2_SHEET_ID = os.environ.get("CASH2_SHEET_ID", "").strip()   # شيت تاني للخزنة (اختياري)


def _append_spaced(ws, lines):
    """بيكتب القيد بعد آخر صف مكتوب وبينه وبين القيد اللي قبله سطر فاضي واحد."""
    last = len(ws.col_values(1))
    start = last + 2 if last > 1 else last + 1
    need = start + len(lines) - 1
    if need > ws.row_count:
        ws.add_rows(need - ws.row_count + 100)
    ws.update(f"A{start}", lines, value_input_option="USER_ENTERED")
    return last, start


def gl_append(lines):
    """بيكتب القيود في GL_Entries (بسطر فاضي قبل كل قيد) وبيتأكد إنها اتكتبت. لو فشل بيرمي خطأ يوصلك على تليجرام."""
    ws = sheet.worksheet(GL_TAB)
    before, start = _append_spaced(ws, lines)
    after = len(ws.col_values(1))
    if after < start + len(lines) - 1:
        raise RuntimeError(f"القيد ماتسجلش في {GL_TAB}: الصفوف كانت {before} وبقت {after} (المفروض تزيد {len(lines)}).")
    _GL_CACHE["t"] = 0
    try:
        custody_tabs_log(lines)
    except Exception as e:
        logging.warning("custody tabs log failed: %s", e)
    if GL2_SHEET_ID:
        try:
            ws2 = gspread.authorize(creds).open_by_key(GL2_SHEET_ID).worksheet(GL2_TAB)
            _append_spaced(ws2, lines)
        except Exception as e:
            logging.warning("second GL sheet failed: %s", e)
    try:
        kick_reports()
    except Exception as e:
        logging.warning("reports refresh failed: %s", e)


def mirror_cash_row(tab, row):
    """نسخة من حركة الخزنة في الشيت التاني (لو متظبط)."""
    if not CASH2_SHEET_ID:
        return
    try:
        ws2 = gspread.authorize(creds).open_by_key(CASH2_SHEET_ID).worksheet(tab)
        ws2.append_row(row, value_input_option="USER_ENTERED", table_range="A1")
    except Exception as e:
        logging.warning("second cash sheet failed: %s", e)


def find_user(uid):
    """(الاسم, الدور) من تاب Users_IDs."""
    for i, n, role in read_users():
        if i == uid:
            return n, role
    return None, ""


def read_lists():
    rows = sheet.worksheet("Lists").get_all_values()[1:]

    def col(i):
        return [r[i].strip() for r in rows if len(r) > i and r[i].strip()]

    def first(i):
        c = col(i)
        return c[0] if c else ""

    bank_disp, bank_l3 = col(2), [r[3].strip() if len(r) > 3 else "" for r in rows][:len(col(2))]
    l3_map = dict(zip(bank_disp, bank_l3))
    # فصل البنوك عن المحافظ: أي اسم فيه "محفظ" يتحسب محفظة، والباقي بنوك
    # محفظة مستر جمعة بتتضاف مرة واحدة بس (لو موجودة في الشيت بنشيلها ونضيفها من الكود)
    banks, wallets = [], []
    for x in bank_disp:
        if "جمعه" in norm(x):
            continue
        (wallets if is_wallet(x) else banks).append(x)
    banks = list(dict.fromkeys(banks))
    wallets = list(dict.fromkeys(wallets)) + [GOMAA_WALLET]
    cash_disp, cash_l3 = col(4), col(5)
    return {
        "clients": col(0),
        "sellers": col(1),
        "bank": banks,
        "wallet": wallets,
        "bank_l3": l3_map,
        "wallet_l3": l3_map,
        "cash": cash_disp,
        "cash_l3": dict(zip(cash_disp, cash_l3)),
        "types": col(6),
        "cc": first(7),
        "l1": first(8),
        "l2_bank": first(9),
        "l2_cash": first(10),
        "l2_client": first(11),
    }


USERS_TAB = "Users_IDs"


def role_key(text):
    """بيحوّل اللي مكتوب في عمود الدور لدور معروف."""
    n = norm(text)
    if not n:
        return ""
    if "operation" in n or "اوبريشن" in n or "عمليات" in n:
        return "operation"
    if (("مسؤول" in n or "مسوول" in n or "مسئول" in n) and "مبيعات" in n) or "sales_mgr" in n:
        return "sales_mgr"
    if "تركيب" in n or "install" in n:
        return "install_mgr"
    if "ceo" in n or "سي اي او" in n or "المدير التنفيذي" in n:
        return "ceo"
    if n == "sm" or "اس ام" in n or "مدير مبيعات" in n or "مدير المبيعات" in n:
        return "sm"
    if "خزن" in n and "فرع" in n:
        return "cash_sub"
    if "خزن" in n and "رئيس" in n:
        return "cash_main"
    if "محاسب" in n or "admin" in n or "ادمن" in n:
        return "accountant"
    if "بياع" in n or "seller" in n:
        return "seller"
    return ""


def read_users():
    """تاب Users_IDs: A الاسم، B Telegram ID، C الدور، D ملاحظات. بيرجّع [(id, اسم, دور)]."""
    out = []
    for r in sheet.worksheet(USERS_TAB).get_all_values()[1:]:
        if len(r) > 1 and r[0].strip():
            digits = re.sub(r"\D", "", r[1].translate(AR_DIGITS))
            if digits:
                out.append((int(digits), r[0].strip(), role_key(r[2]) if len(r) > 2 else ""))
    return out


def read_seller_ids():
    """{id: اسم} للبياعين بس (الدور = بياع)."""
    return {str(i): n for i, n, role in read_users() if role in ("seller", "sales_mgr", "install_mgr")}


def ensure_ids_tab():
    try:
        sheet.worksheet(USERS_TAB)
        return
    except gspread.WorksheetNotFound:
        pass
    try:                                  # ترحيل الشيت القديم Sellers_IDs: بنغير اسمه وبنضيف عمود الدور
        ws = sheet.worksheet("Sellers_IDs")
        ws.update_title(USERS_TAB)
        n = len(ws.get_all_values())
        ws.insert_cols([["الدور"] + ["بياع"] * (n - 1)], col=3)
        ws.update("A1", [["الاسم", "Telegram ID"]])
        return
    except gspread.WorksheetNotFound:
        pass
    names = read_lists()["sellers"]
    ws = sheet.add_worksheet(title=USERS_TAB, rows=max(50, len(names) + 10), cols=4)
    ws.update("A1", [["الاسم", "Telegram ID", "الدور", "ملاحظات"]] + [[n, "", "بياع", ""] for n in names])


def register_unknown(uid, tg_name):
    """أي حد جديد يفتح البوت بيتضاف في Users_IDs من غير دور، وأنت تكتب دوره."""
    if any(i == uid for i, _, _ in read_users()):
        return False
    sheet.worksheet(USERS_TAB).append_row([tg_name or f"مستخدم {uid}", str(uid), "", "سجّل نفسه تلقائي - اكتب الدور"],
                                          value_input_option="USER_ENTERED", table_range="A1")
    return True


def markup(rows, back=True):
    rows = [list(r) for r in rows]
    if back:
        rows.append([InlineKeyboardButton("⬅️ رجوع", callback_data="back")])
    return InlineKeyboardMarkup(rows)


def kb(items, prefix, back=True, cols=1, labels=None):
    labels = labels or items
    btns = [InlineKeyboardButton(t, callback_data=f"{prefix}{i}") for i, t in enumerate(labels)]
    rows = [btns[i:i + cols] for i in range(0, len(btns), cols)]
    return markup(rows, back)


def types_kb(items):
    """نوع الدفعة: الأول (دفعة التعاقد) لوحده في سطر، والباقي اتنين اتنين جنب بعض."""
    btns = [InlineKeyboardButton(nopct(t), callback_data=f"t:{i}") for i, t in enumerate(items)]
    rows = [btns[:1]] + [btns[i:i + 2] for i in range(1, len(btns), 2)]
    return markup(rows)


async def say(update: Update, text, markup=None):
    if update.callback_query:
        await update.callback_query.answer()
        try:
            await update.callback_query.edit_message_text(text, reply_markup=markup)
            return
        except Exception:
            pass
        await update.callback_query.message.reply_text(text, reply_markup=markup)
    else:
        await update.message.reply_text(text, reply_markup=markup)


# ---------------- مسح رسايل الطلب (الصورة والقوايم) بعد التأكيد أو الرفض ----------------
MSGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "owner_msgs.json")
try:
    with open(MSGS_FILE, encoding="utf-8") as _fh:
        _MSGS = json.load(_fh)
except Exception:
    _MSGS = {}


def _msgs_save():
    try:
        with open(MSGS_FILE, "w", encoding="utf-8") as fh:
            json.dump(_MSGS, fh)
    except Exception as e:
        logging.warning("couldn't save owner_msgs.json: %s", e)


def msg_track(key, m):
    """بنحفظ رقم الرسالة اللي اتبعتت للمدير (الطلب أو الصورة) عشان نمسحها بعد القرار."""
    if m is not None:
        _MSGS.setdefault(key, []).append([m.chat_id, m.message_id])
        _msgs_save()


async def msg_purge(bot, key, update=None):
    """بيمسح كل رسايل الطلب ده ما عدا الرسالة اللي بتتحول لبيان التأكيد أو الرفض."""
    keep = None
    q = update.callback_query if update is not None else None
    if q is not None and q.message is not None:
        keep = [q.message.chat_id, q.message.message_id]
    for chat, mid in _MSGS.pop(key, []):
        if keep and [chat, mid] == keep:
            continue
        try:
            await bot.delete_message(chat, mid)
        except Exception:
            pass
    _msgs_save()


# ---------------- Seller flow ----------------
def summary(d):
    bank = f" ({d['other_bank']})" if d["other_bank"] else ""
    return (
        "راجع الطلب:\n"
        f"البياع: {d['seller']}\n"
        f"العميل: {d['client']}\n"
        f"النوع: {nopct(d['ptype'])}\n"
        f"المبلغ: {d['amount']:,.2f}\n"
        f"الدفع: {d['method']}{bank}\n"
        + (f"رقم الوصل: {d['voucher']}\n" if d.get("voucher") else "")
        + ("المرفق: ✅\n" if d.get("att") else "")
        + f"التاريخ: {d['date']}\n"
        f"ملاحظات: {d['notes'] or '-'}"
    )


async def prompt(update, context, state):
    d = context.user_data["d"]
    L = context.user_data["L"]
    if state == SELLER:
        solo_keys = {norm(x) for x in SOLO_SELLERS}
        names = L["sellers"]
        solo = [i for i, n in enumerate(names) if norm(n) in solo_keys]
        rest = [i for i in range(len(names)) if i not in solo]
        titles = {norm(k): v for k, v in SELLER_TITLES.items()}
        label = lambda i: names[i] + (f" ({titles[norm(names[i])]})" if norm(names[i]) in titles else "")
        btn = lambda i: InlineKeyboardButton(label(i), callback_data=f"s:{i}")
        rows = [[btn(i)] for i in solo] + [[btn(j) for j in rest[k:k + 2]] for k in range(0, len(rest), 2)]
        await say(update, "أهلاً ❀\nصلي على محمد ﷺ ❀\n\nواختار اسمك:", markup(rows))
    elif state == SEARCH:
        await say(update, "اكتب اسم العميل بالظبط زي ما عايزه يتسجل:", markup([], back=not context.user_data.get("fixed_seller")))
    elif state == PTYPE:
        await say(update, "نوع الدفعة:", types_kb(L["types"]))
    elif state == AMOUNT:
        await say(update, "اكتب المبلغ (أرقام بس):", markup([]))
    elif state == KIND:
        await say(update, "الدفع كان فين؟", markup([[
            InlineKeyboardButton("خزينة", callback_data="k:cash"),
            InlineKeyboardButton("بنك", callback_data="k:bank"),
            InlineKeyboardButton("فودافون كاش", callback_data="k:wallet"),
        ]]))
    elif state == METHOD:
        items = L[d["kind"]]
        labels = [wallet_label(x) for x in items] if d["kind"] == "wallet" else None
        text = "اختار:"
        if labels:
            lines = [f"{x}\n({WALLET_NUMBERS[x]})" for x in labels if x in WALLET_NUMBERS]
            if lines:
                text += "\n\n" + "\n\n".join(lines)
        await say(update, text, kb(items, "m:", labels=labels))
    elif state == OTHER_BANK:
        await say(update, "اكتب اسم البنك:", markup([]))
    elif state == ATTACH:
        await say(update, ATT_PROMPT,
                  markup([[InlineKeyboardButton("تخطي", callback_data="at:skip")]]))
    elif state == P_VOUCH:
        await say(update, "اكتب رقم الوصل (لازم):", markup([]))
    elif state == DATE:
        await say(update, "تاريخ الدفعة؟ اضغط اليوم أو اكتبه بالشكل ده: 25/10/2026",
                  markup([[InlineKeyboardButton("اليوم", callback_data="d:today")]]))
    elif state == NOTES:
        await say(update, "أي ملاحظات؟ اكتبها أو اضغط الزرار:",
                  markup([[InlineKeyboardButton("من غير ملاحظات", callback_data="n:skip")]]))
    elif state == CONFIRM:
        await say(update, summary(d), markup([[
            InlineKeyboardButton("إرسال", callback_data="go"),
            InlineKeyboardButton("إلغاء", callback_data="cancel"),
        ]]))


async def go(update, context, state):
    d = context.user_data.get("d") or {}
    if d.get("preset") and state in (AMOUNT, KIND, METHOD, OTHER_BANK, DATE, NOTES, ATTACH):
        # المبلغ والحساب والتاريخ والبيان والمرفق جاية من التحويل: نروح لرقم الوصل (لو خزينة) أو للتأكيد
        state = P_VOUCH if d.get("kind") == "cash" and not d.get("voucher") else CONFIRM
    context.user_data.setdefault("hist", []).append(state)
    await prompt(update, context, state)
    return state


async def go_back(update, context):
    hist = context.user_data.get("hist")
    if not hist or "L" not in context.user_data:
        await update.callback_query.answer()
        return None
    if len(hist) <= 1:              # أول خطوة: رجوع للقايمة الرئيسية
        uid = update.effective_user.id
        tq = (context.user_data.get("d") or {}).get("tq")
        cx = (context.user_data.get("d") or {}).get("cx")
        context.user_data.clear()
        if tq and await tq_back_to_menu(update, tq):
            return ConversationHandler.END
        if cx and await cx_back_menu(update, cx):
            return ConversationHandler.END
        if uid in ADMINS:
            await say(update, "تفتح إيه؟", menu_markup())
        else:
            await update.callback_query.answer()
        return ConversationHandler.END
    hist.pop()
    state = hist[-1]
    await prompt(update, context, state)
    return state


def menu_markup():
    rows = [
        [InlineKeyboardButton("التحصيلات (دفعة عميل)", callback_data="menu:pay"),
         InlineKeyboardButton("موردين", callback_data="menu:sup")],
        [InlineKeyboardButton("عهدة", callback_data="menu:cus"),
         InlineKeyboardButton("سلفة", callback_data="menu:adv")],
        [InlineKeyboardButton("مصروفات", callback_data="menu:exp"),
         InlineKeyboardButton("إيرادات", callback_data="menu:inc")],
        [InlineKeyboardButton("مبيعات", callback_data="menu:sale"),
         InlineKeyboardButton("تحويل بين الحسابات", callback_data="menu:xfer")],
        [InlineKeyboardButton("🏢 المشتريات (شراء أصل)", callback_data="menu:buy"),
         InlineKeyboardButton("🏦 الخزنة", callback_data="menu:cash")],
        [InlineKeyboardButton("📋 المعلّقات", callback_data="pd:open"),
         InlineKeyboardButton("📝 ملاحظات المدير", callback_data="mn:menu")],
        [InlineKeyboardButton("✏️ تعديل / حذف قيد", callback_data="menu:edit"),
         InlineKeyboardButton("📊 ملخص", callback_data="menu:sum")],
        [InlineKeyboardButton("🔎 بحث", callback_data="menu:search"),
         InlineKeyboardButton("🔄 مزامنة مع اليومية", callback_data="sy:run")],
        [InlineKeyboardButton("🧪 فحص التكرار", callback_data="sy:dup"),
         InlineKeyboardButton("📥 تحديث بلصق", callback_data="pz:ask")],
    ]
    if REAL_UID.get() == OWNER_ID:
        rows.append([InlineKeyboardButton("🏦 سحب الخزنة الفرعية", callback_data="sb:ask"),
                     InlineKeyboardButton("👥 العمل كمستخدم", callback_data="as:list")])
    return InlineKeyboardMarkup(rows)


async def apply_commands(bot, uid, admin):
    try:
        cmds = [BotCommand("pay", "التحصيلات (دفعة عميل)")]
        if admin:
            cmds = [BotCommand("pay", "التحصيلات (دفعة عميل)"), BotCommand("supplier", "موردين"), BotCommand("custody", "عهدة"),
                    BotCommand("advance", "سلفة"), BotCommand("expense", "مصروفات"), BotCommand("income", "إيرادات"), BotCommand("sale", "مبيعات"),
                    BotCommand("custody_return", "رد عهدة"), BotCommand("advance_return", "رد سلفة"),
                    BotCommand("transfer", "تحويل بين الحسابات"), BotCommand("buy", "شراء أصل"), BotCommand("editentry", "تعديل / حذف قيد"), BotCommand("newentry", "إنشاء قيد"),
                    BotCommand("treasury", "الخزنة"), BotCommand("search", "بحث")]
            if uid == OWNER_ID:
                cmds += [BotCommand("broadcast", "إذاعة رسالة"), BotCommand("actas", "العمل كمستخدم"), BotCommand("me", "رجوع لحسابي"),
                         BotCommand("azkar", "إرسال الأذكار دلوقتي")]
        elif uid in CEO_IDS or uid == GOMAA_ID or uid in CASH_MAIN_IDS or uid in CASH_SUB_IDS:
            cmds = [BotCommand("start", "القايمة")]
        await bot.set_my_commands(cmds, scope=BotCommandScopeChat(uid))
    except Exception as e:
        logging.warning("apply_commands failed: %s", e)


MENU_BTN = "☰ القائمة"
START_RE = r"^\s*(\.|☰ القائمة)\s*$"      # النقطة أو زرار القايمة = /start


def menu_kb():
    btn = [[KeyboardButton(MENU_BTN)]]
    try:
        return ReplyKeyboardMarkup(btn, resize_keyboard=True, is_persistent=True)
    except TypeError:       # نسخ مكتبة قديمة
        return ReplyKeyboardMarkup(btn, resize_keyboard=True)


async def pin_menu_once(update, context):
    """زرار «☰ القائمة» بيتثبّت تحت الشاشة (بيفضل ظاهر مع البوت). بيتبعت مرة لكل واحد."""
    uid = update.effective_user.id
    if MINIAPP_URL and uid not in ADMINS:
        return
    shown = context.application.bot_data.setdefault("kb_shown", set())
    if uid in shown:
        return
    shown.add(uid)
    try:
        await update.effective_message.reply_text(
            "📌 زرار «☰ القائمة» اتثبّت تحت الشاشة. اضغطه (أو ابعت نقطة .) في أي وقت عشان تفتح البوت.",
            reply_markup=menu_kb())
    except Exception as e:
        logging.warning("pin menu failed: %s", e)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/start بيفتح للكل حسب دوره: محاسب، CEO، SM، أمين خزنة، بياع."""
    await ensure_roles()
    await pin_menu_once(update, context)
    uid = update.effective_user.id
    if uid in ADMINS:
        context.user_data.clear()
        await apply_commands(context.bot, uid, True)
        hello = f"{greeting()} {ADMIN_USERS.get(uid, update.effective_user.first_name)} ❀\nصلي على محمد ﷺ ❀"
        if MINIAPP_URL:
            await update.message.reply_text(hello + "\n\nتحب تشتغل بإيه؟", reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📋 رسائل عادية", callback_data="md:n"),
                InlineKeyboardButton("📝 رسائل فورم", callback_data="md:f")]]))
            return ConversationHandler.END
        await update.message.reply_text(hello + "\n\nتفتح إيه؟", reply_markup=menu_markup())
        return ConversationHandler.END
    if uid in CEO_IDS:
        context.user_data.clear()
        await apply_commands(context.bot, uid, False)
        if MINIAPP_URL:
            await update.message.reply_text(f"{greeting()} {CEO_NAME} ❀\nصلي على محمد ﷺ ❀\n\nاختار من الفورمين اللي تحت 👇", reply_markup=role_kb(CEO_BTNS))
            return ConversationHandler.END
        await update.message.reply_text(f"{greeting()} {CEO_NAME} ❀\nصلي على محمد ﷺ ❀\n\nحضرتك تختار إيه؟", reply_markup=ceo_menu(uid))
        return ConversationHandler.END
    if uid == GOMAA_ID:
        await apply_commands(context.bot, uid, False)
        if MINIAPP_URL:
            await update.message.reply_text(f"{greeting()} م/ محمد جمعة ❀\nصلي على محمد ﷺ ❀\n\nاختار من الفورمين اللي تحت 👇", reply_markup=role_kb(GM_BTNS))
            return ConversationHandler.END
        await update.message.reply_text(f"{greeting()} م/ محمد جمعة ❀\nصلي على محمد ﷺ ❀\n\nحضرتك تختار إيه؟", reply_markup=gomaa_menu())
        return ConversationHandler.END
    if uid in OPERATION_IDS:
        await update.message.reply_text(f"{greeting()} {update.effective_user.first_name} ❀\nصلي على محمد ﷺ ❀\n\nهيوصلك هنا إشعار بكل تحصيل بياع يتسجل في القيود.")
        return ConversationHandler.END
    if uid in CASH_MAIN_IDS or uid in CASH_SUB_IDS:
        await apply_commands(context.bot, uid, False)
        which = "الرئيسية" if uid in CASH_MAIN_IDS else "الفرعية"
        nm = (await run(find_user, uid))[0] or update.effective_user.first_name
        await update.message.reply_text(f"{greeting()} {nm} ❀\nصلي على محمد ﷺ ❀")
        return await cash_home(update, context)
    me_name, me_role = None, ""
    try:
        me_name, me_role = await run(find_user, uid)
    except Exception as e:
        logging.warning("role lookup failed: %s", e)
    if MINIAPP_URL and me_role == "sales_mgr":
        context.user_data.clear()
        pay_url = mini_url({"type": "pay", "seller": me_name, "vals": {}})
        await update.message.reply_text(f"{greeting()} {update.effective_user.first_name} ❀\nصلي على محمد ﷺ ❀\n\nاختار من اللي تحت 👇", reply_markup=mh_kb(pay_url))
        return ConversationHandler.END
    if me_role in ("install_mgr", "sales_mgr") or holder_of(me_name)[0]:
        context.user_data.clear()
        try:
            await context.bot.set_my_commands([BotCommand("start", "القايمة"), BotCommand("pay", "التحصيلات (دفعة عميل)")],
                                              scope=BotCommandScopeChat(uid))
        except Exception as e:
            logging.warning("holder commands failed: %s", e)
        await send_holder_menu(update, context)
        return ConversationHandler.END
    return await start_pay(update, context)


async def coming_soon(update, context):
    if update.effective_user.id not in ADMINS:
        return
    if update.callback_query:
        await update.callback_query.answer()
    await update.effective_message.reply_text("القسم ده لسه بنجهزه. ابعتلي القيود بتاعته ونبدأ.")


async def start_pay(update: Update, context: ContextTypes.DEFAULT_TYPE, att=None):
    await ensure_roles()
    context.user_data.clear()
    uid = update.effective_user.id
    context.user_data["L"] = await run(read_lists)
    context.user_data["d"] = {"uid": uid}
    context.user_data["hist"] = []
    if uid in ADMINS:  # المحاسب يقدر يسجّل لأي بياع: فورم فيها اختيار البياع
        return await owner_form_open(update, context, "opay")
    name = (await run(read_seller_ids)).get(str(uid))
    if not name:
        context.user_data.clear()
        try:
            new = await run(register_unknown, uid, update.effective_user.full_name)
        except Exception as e:
            logging.warning("register_unknown failed: %s", e)
            new = False
        if new:
            try:
                await context.bot.send_message(OWNER_ID, f"👤 مستخدم جديد فتح البوت: {update.effective_user.full_name} ({uid}).\nاتضاف في تاب Users_IDs، اكتب دوره هناك.")
            except Exception:
                pass
        await update.effective_message.reply_text(
            f"{greeting()} {update.effective_user.first_name} ❀\nرقمك على تليجرام ({uid}) اتسجّل عندنا، وحسابك لسه مش مفعّل.\n"
            "استنى لحد ما مسؤول الحسابات يفعّله، وبعدها ابعت /pay."
        )
        return ConversationHandler.END
    if not att:
        await update.effective_message.reply_text(f"{greeting()} {update.effective_user.first_name} ❀\nصلي على محمد ﷺ ❀")
    return await pay_form_open(update, context, name, att)


async def my_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"رقمك على تليجرام: {update.effective_user.id}")


async def pick_seller(update, context):
    L = context.user_data["L"]
    i = int(update.callback_query.data.split(":")[1])
    context.user_data["d"]["seller"] = L["sellers"][i]
    return await go(update, context, SEARCH)


async def search_client(update, context):
    name = update.message.text.strip()
    if not name:
        await update.message.reply_text("اكتب اسم العميل:")
        return SEARCH
    context.user_data["d"]["client"] = name  # بيتسجل زي ما اتكتب بالظبط
    return await go(update, context, PTYPE)


async def pick_type(update, context):
    L = context.user_data["L"]
    i = int(update.callback_query.data.split(":")[1])
    context.user_data["d"]["ptype"] = L["types"][i]
    return await go(update, context, AMOUNT)


async def got_amount(update, context):
    t = update.message.text.translate(AR_DIGITS).replace(",", "").strip()
    try:
        amount = float(t)
        assert amount > 0
    except Exception:
        await update.message.reply_text("المبلغ غلط. اكتب أرقام بس، مثلًا 25000:")
        return AMOUNT
    context.user_data["d"]["amount"] = amount
    return await go(update, context, KIND)


async def pick_kind(update, context):
    context.user_data["d"]["kind"] = update.callback_query.data.split(":")[1]
    return await go(update, context, METHOD)


async def pick_method(update, context):
    d = context.user_data["d"]
    L = context.user_data["L"]
    i = int(update.callback_query.data.split(":")[1])
    name = L[d["kind"]][i]
    d["method"] = name
    d["other_bank"] = ""
    d["l3"] = L[d["kind"] + "_l3"].get(name, "")
    if name.startswith("أخرى"):
        d["l3"] = ""
        return await go(update, context, OTHER_BANK)
    return await seller_after_method(update, context)


async def seller_after_method(update, context):
    d = context.user_data["d"]
    if d["kind"] == "cash":
        return await go(update, context, P_VOUCH)      # رقم الوصل لازم
    return await go(update, context, DATE)


async def got_other_bank(update, context):
    d = context.user_data["d"]
    d["other_bank"] = update.message.text.strip()
    d["l3"] = d["other_bank"]
    return await seller_after_method(update, context)


async def got_date(update, context):
    d = context.user_data["d"]
    if update.callback_query:
        d["date"] = datetime.now().strftime("%Y-%m-%d")
    else:
        t = update.message.text.translate(AR_DIGITS).strip()
        try:
            d["date"] = datetime.strptime(t, "%d/%m/%Y").strftime("%Y-%m-%d")
        except ValueError:
            await update.message.reply_text("التاريخ غلط. اكتبه كده: 25/10/2026")
            return DATE
    if d.get("tq"):         # البيان والمرفق جايين من التحويل
        return await go(update, context, CONFIRM)
    return await go(update, context, NOTES)


async def got_notes(update, context):
    d = context.user_data["d"]
    d["notes"] = "" if update.callback_query else update.message.text.strip()
    if d.get("att"):
        return await go(update, context, CONFIRM)
    return await go(update, context, ATTACH)


def save_pending(d):
    ws = sheet.worksheet("Pending")
    if ws.col_count < 17:                       # عمود Q = رقم المشروع (اختياري)
        ws.add_cols(17 - ws.col_count)
        ws.update("Q1", [["رقم المشروع"]])
    rid = len(ws.col_values(1))  # header counts as 1 -> first request = 1
    ws.append_row(
        [rid, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), d["date"], d["ptype"], d["amount"],
         d["client"], d["seller"], d["method"], d["other_bank"], d["notes"], d["uid"], "معلّق", "", "",
         att_ref(d.get("att")), _safe_cell(d.get("voucher", "")), _safe_cell(d.get("project", ""))],
        value_input_option="USER_ENTERED",
    )
    return rid


async def send_request(update, context):
    d = context.user_data["d"]
    rid = await run(save_pending, d)
    if d.get("tq"):
        try:
            await run(tq_mark, d["tq"], f"اتحول لطلب تحصيل رقم {rid}")
        except Exception as e:
            logging.warning("tq_mark failed: %s", e)
    if d.get("cx"):
        try:
            await run(cx_mark, d["cx"], f"اتحول لطلب تحصيل رقم {rid}")
        except Exception as e:
            logging.warning("cx_mark failed: %s", e)
    bank = f" ({d['other_bank']})" if d["other_bank"] else ""
    m_req = await context.bot.send_message(
        OWNER_ID,
        f"طلب جديد رقم {rid}\n"
        f"البياع: {d['seller']}\nالعميل: {d['client']}\nالنوع: {nopct(d['ptype'])}\n"
        f"المبلغ: {d['amount']:,.2f}\nالدفع: {d['method']}{bank}\n"
        f"التاريخ: {d['date']}\nملاحظات: {d['notes'] or '-'}"
        + (f"\nرقم الوصل: {d['voucher']}" if d.get("voucher") else "")
        + (f"\nرقم المشروع: {d['project']}" if d.get("project") else "")
        + ("\nالمرفق: ✅ (الصورة تحت)" if d.get("att") else ""),
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("تأكيد", callback_data=f"ok:{rid}"),
            InlineKeyboardButton("رفض", callback_data=f"no:{rid}"),
        ]]),
    )
    msg_track(f"r{rid}", m_req)
    if d.get("att"):
        await send_att(context, OWNER_ID, att_ref(d["att"]), f"مرفق الطلب رقم {rid} ({d['seller']})", key=f"r{rid}")
    if d.get("tq"):          # الطلب الأصلي (تحويل / خزنة) اتحوّل لطلب تحصيل: نمسح رسايله
        await msg_purge(context.bot, f"t{d['tq']}")
    if d.get("cx"):
        await msg_purge(context.bot, f"c{d['cx']}")
    await say(update, f"تم إرسال الطلب رقم {rid}. هيوصلك رد بعد المراجعة."
              + ("\nوالصورة هتتعرض على مسؤول الحسابات للمراجعة." if d.get("att") else ""))
    if d.get("from_form") and MINIAPP_URL:
        try:
            await again_menu(context, d)
        except Exception as e:
            logging.warning("again menu failed: %s", e)
    return ConversationHandler.END


async def cancel(update, context):
    context.user_data.clear()
    await say(update, "اتلغى. ابعت /new لو عايز تبدأ من جديد.")
    return ConversationHandler.END


# ---------------- Owner flow ----------------
def find_request(rid):
    ws = sheet.worksheet("Pending")
    cell = ws.find(str(rid), in_column=1)
    if not cell:
        return None, None, None
    return ws, cell.row, ws.row_values(cell.row)


async def owner_confirm(update, context):
    q = update.callback_query
    if q.from_user.id != OWNER_ID:
        await q.answer("الزرار ده للمدير بس", show_alert=True)
        return ConversationHandler.END
    rid = q.data.split(":")[1]
    ws, row, vals = await run(find_request, rid)
    if not row:
        await say(update, "الطلب مش موجود.")
        return ConversationHandler.END
    if len(vals) > 11 and vals[11].strip() != "معلّق":
        await say(update, f"الطلب {rid} اتعالج قبل كده.")
        return ConversationHandler.END
    context.user_data["rid"] = rid
    context.user_data["orig"] = q.message.text or f"الطلب {rid}"
    context.user_data["amsg"] = (q.message.chat_id, q.message.message_id)
    given = (vals[16].strip() if len(vals) > 16 else "")      # رقم المشروع بيظهر في الفورم، فمش بنسأل عليه
    context.user_data["project"] = given
    await ask_accountant(update, context)
    return ACCOUNTANT


def ob_markup():
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ رجوع", callback_data="ob:req")]])


async def owner_back(update, context):
    q = update.callback_query
    if q.from_user.id != OWNER_ID:
        await q.answer("غير مسموح", show_alert=True)
        return None
    rid = context.user_data.get("rid")
    if q.data.endswith(":proj") and rid:
        await say(update, f"الطلب {rid}: اكتب كود المشروع:", ob_markup())
        return PROJECT
    if rid:                     # رجوع لرسالة الطلب الأصلية بأزرارها
        await say(update, context.user_data.get("orig") or f"الطلب {rid}", InlineKeyboardMarkup([[
            InlineKeyboardButton("تأكيد", callback_data=f"ok:{rid}"),
            InlineKeyboardButton("رفض", callback_data=f"no:{rid}")]]))
    context.user_data.clear()
    return ConversationHandler.END


def post_entries(rid, project, user):
    ws, row, v = find_request(rid)
    v = v + [""] * (16 - len(v))
    L = read_lists()
    date, ptype, amount, client, seller, method, other_bank = v[2], v[3], float(v[4]), v[5], v[6], v[7], v[8]
    voucher = str(v[15]).strip()
    number = voucher or rid
    custody = method == GOMAA_WALLET
    if custody:
        l3, l2 = CUSTODY_L3, CUSTODY_L2
    elif method.startswith("أخرى"):
        l3, l2 = other_bank, L["l2_bank"]
    elif method in L["bank_l3"]:
        l3, l2 = L["bank_l3"][method], L["l2_bank"]
    else:
        l3, l2 = L["cash_l3"].get(method, method), L["l2_cash"]
    # columns: user, date, number, statement, L1, L2, L3, L4, fx, rate, debt age, cc1, cc2, client, project, debit, credit
    statement = f"{ptype} العميل {client} تبع {seller}"
    if custody:
        person = CUSTODY_PERSON
        debit = [user, date, number, statement, L["l1"], l2, l3, person, 0, "", "", "", "", "", "", amount, ""]
    else:
        debit = [user, date, number, statement, L["l1"], l2, l3, "", 0, "", "", L["cc"], seller, client, project, amount, ""]
    credit = [user, date, number, statement, L["l1"], L["l2_client"], client, ptype, 0, "", "", L["cc"], seller, client, project, "", amount]
    gl_append([debit, credit])
    _GL_CACHE["t"] = 0
    if l2 == L["l2_cash"]:
        try:
            cash_row(l3, date, voucher, statement, None, amount, "دفعة عميل", user)
        except Exception as e:
            logging.warning("cashbox log failed: %s", e)
    ws.update(f"L{row}:N{row}", [["تم", project, datetime.now().strftime("%Y-%m-%d %H:%M:%S")]])
    return v[10]


async def ask_accountant(update, context):
    markup = InlineKeyboardMarkup([list(r) for r in kb(ACCOUNTANTS, "u:", back=False).inline_keyboard]
                                  + [[InlineKeyboardButton("⬅️ رجوع", callback_data="ob:req")]])
    am = context.user_data.get("amsg")
    if am:                         # نفس رسالة الطلب بتتحول، من غير رسايل زيادة
        try:
            await context.bot.edit_message_text(chat_id=am[0], message_id=am[1], text="مين المحاسب اللي بيسجّل؟", reply_markup=markup)
            return
        except Exception as e:
            logging.info("edit accountant prompt failed: %s", e)
    target = update.message or update.callback_query.message
    await target.reply_text("مين المحاسب اللي بيسجّل؟", reply_markup=markup)


async def got_project(update, context):
    context.user_data["project"] = update.message.text.strip()
    try:
        await update.message.delete()
    except Exception:
        pass
    await ask_accountant(update, context)
    return ACCOUNTANT


def pending_ref(rid):
    ws, row, v = find_request(rid)
    v = v + [""] * (16 - len(v))
    return ws, row, str(v[14]), v[6], v


def req_block(rid, v, head="تم تسجيل طلبك رقم", extra="", mark="✅"):
    """بيان الطلب: عنوان + التفاصيل + (سطور زيادة) + علامة صح."""
    v = list(v or []) + [""] * 17
    bank = f" ({v[8]})" if v[8] else ""
    try:
        amt = f"{float(v[4]):,.2f}"
    except Exception:
        amt = str(v[4])
    return (f"{head} {rid}\n"
            f"البياع: {v[6]}\nالعميل: {v[5]}\nالنوع: {nopct(v[3])}\n"
            f"المبلغ: {amt}\nالدفع: {v[7]}{bank}\n"
            f"التاريخ: {v[2]}\nملاحظات: {str(v[9]).strip() or '-'}"
            + (f"\nرقم الوصل: {str(v[15]).strip()}" if str(v[15]).strip() else "")
            + (f"\nرقم المشروع: {str(v[16]).strip()}" if str(v[16]).strip() else "")
            + (f"\n{extra}" if extra else "")
            + "\n\n" + mark)


async def notify_operation(context, rid, project, user):
    """بعد ما تحصيل البياع يتسجل في قيد اليومية: نبعته ليوزر operation على تليجرام."""
    await ensure_roles()
    if not OPERATION_IDS:
        return
    try:
        ws, row, v = await run(find_request, rid)
        v = (v or []) + [""] * 16
        ref = str(v[14]).strip()
        text = (f"✅ تحصيل اتسجّل في قيد اليومية\n"
                f"رقم الطلب: {rid}\n"
                f"البياع: {v[6]}\nالعميل: {v[5]}\nالنوع: {nopct(v[3])}\n"
                f"المبلغ: {float(v[4]):,.2f}\n"
                f"الدفع: {v[7]}" + (f" ({v[8]})" if v[8] else "") + "\n"
                f"التاريخ: {v[2]}" + (f"\nكود المشروع: {project}" if project else "") + f"\nالمحاسب: {user}"
                + (f"\nرقم الوصل: {v[15]}" if str(v[15]).strip() else ""))
    except Exception as e:
        logging.warning("operation notice build failed: %s", e)
        return
    for op in list(OPERATION_IDS):
        try:
            await context.bot.send_message(op, text)
            if ref.startswith("tg:"):
                await send_att(context, op, ref, f"مرفق الطلب رقم {rid} ({v[6]})")
        except Exception as e:
            logging.warning("operation notice failed for %s: %s", op, e)


async def pick_accountant(update, context):
    user = ACCOUNTANTS[int(update.callback_query.data.split(":")[1])]
    project = context.user_data.get("project")
    rid = context.user_data.get("rid")
    seller_id = await run(post_entries, rid, project, user)
    note = ""
    pv = None
    try:
        ws, row, ref, seller, pv = await run(pending_ref, rid)
        if ref.startswith("tg:"):
            path = await save_ref(context, "التحصيلات", seller, ref, f"{pv[3]} العميل {pv[5]} تبع {pv[6]}", str(pv[2]))
            if path:
                await run(ws.update_acell, f"O{row}", path)
                note = "\nالمرفق اتحفظ ✅"
            else:
                note = "\n⚠️ القيد اتسجّل بس المرفق ماتحفظش."
    except Exception as e:
        logging.warning("pending attachment failed: %s", e)
    if pv:
        await say(update, req_block(rid, pv, head="تم تسجيل الطلب رقم",
                                    extra=("" if (str((pv + [""] * 17)[16]).strip() or not project) else f"كود المشروع: {project}\n") + f"المحاسب: {user}" + note.rstrip()))
    else:
        await say(update, f"تم تسجيل الطلب {rid} في GL_Entries (مشروع {project}، المستخدم {user}).{note}")
    await msg_purge(context.bot, f"r{rid}", update)
    await notify_operation(context, rid, project, user)
    try:
        await context.bot.send_message(int(seller_id), req_block(rid, pv) if pv else f"طلبك رقم {rid} اتأكد وتسجّل.")
    except Exception:
        pass
    return ConversationHandler.END


def mark_rejected(rid):
    ws, row, v = find_request(rid)
    if not row:
        return None
    ws.update(f"L{row}:N{row}", [["مرفوض", "", datetime.now().strftime("%Y-%m-%d %H:%M:%S")]])
    return v[10] if len(v) > 10 else None


async def owner_reject(update, context):
    q = update.callback_query
    if q.from_user.id != OWNER_ID:
        await q.answer("الزرار ده للمدير بس", show_alert=True)
        return
    rid = q.data.split(":")[1]
    seller_id = await run(mark_rejected, rid)
    try:
        _ws, _row, _v = await run(find_request, rid)
        stmt = req_block(rid, _v, head="تم رفض الطلب رقم", mark="❌ اترفض") if _row else f"الطلب {rid} اترفض ❌"
    except Exception:
        stmt = f"الطلب {rid} اترفض ❌"
    await say(update, stmt)
    await msg_purge(context.bot, f"r{rid}", update)
    if seller_id:
        try:
            await context.bot.send_message(int(seller_id), f"طلبك رقم {rid} اترفض. اتواصل مع مسؤول الحسابات.")
        except Exception:
            pass

# ---------------- Suppliers flow (للمحاسبين بس) ----------------
def smarkup(rows, back=True):
    rows = [list(r) for r in rows]
    if back:
        rows.append([InlineKeyboardButton("⬅️ رجوع", callback_data="sback")])
    return InlineKeyboardMarkup(rows)


def pairs(btns, first_alone=False):
    if first_alone:
        return [btns[:1]] + [btns[i:i + 2] for i in range(1, len(btns), 2)]
    return [btns[i:i + 2] for i in range(0, len(btns), 2)]


async def sprompt(update, context, state):
    d = context.user_data["sd"]
    L = context.user_data["sL"]
    if state == S_SUP:
        btns = [InlineKeyboardButton(n, callback_data=f"ss:{i}") for i, n in enumerate(SUPPLIERS)]
        await say(update, "أهلاً ❀ اختار المورد:", smarkup(pairs(btns)))
    elif state == S_TYPE:
        btns = [InlineKeyboardButton(n, callback_data=f"st:{i}") for i, n in enumerate(SUPPLIER_TYPES)]
        await say(update, f"المورد: {d['supplier']}\nالنوع:", smarkup(pairs(btns, first_alone=True)))
    elif state == S_STL:
        names = ["تسوية عهدة م/ محمد جمعة", "تسوية عهدة م/ محمد حسن", "تسوية عهدة موظف آخر"]
        btns = [InlineKeyboardButton(n, callback_data=f"sx:{i}") for i, n in enumerate(names)]
        await say(update, "تسوية عهدة:", smarkup([[b] for b in btns]))
    elif state == S_SALETYPE:
        btns = [InlineKeyboardButton(x, callback_data=f"sy:{i}") for i, x in enumerate(SALE_TYPES)]
        await say(update, "أهلاً ❀ نوع العملية:", smarkup(pairs(btns)))
    elif state == S_SELLER:
        btns = [InlineKeyboardButton(n, callback_data=f"sl:{i}") for i, n in enumerate(L["sellers"])]
        await say(update, "أهلاً ❀ البياع:", smarkup(pairs(btns)))
    elif state == S_CLIENT:
        await say(update, "اكتب اسم العميل:", smarkup([]))
    elif state == S_PROJECT:
        await say(update, "اكتب رقم المشروع:", smarkup([]))
    elif state == S_EL1:
        l1s, tree, l3m = tree_of(d)
        btns = [InlineKeyboardButton(x.replace("_", " "), callback_data=f"xa:{i}") for i, x in enumerate(l1s)]
        await say(update, "أهلاً ❀ نوع " + ("الإيراد:" if d["flow"] == "inc" else "المصروف:"), smarkup(pairs(btns)))
    elif state == S_EL2:
        l1s, tree, l3m = tree_of(d)
        btns = [InlineKeyboardButton(x.replace("_", " "), callback_data=f"xb:{i}") for i, x in enumerate(tree[d["l1x"]])]
        await say(update, "البند:", smarkup(pairs(btns)))
    elif state == S_EL3:
        l1s, tree, l3m = tree_of(d)
        btns = [InlineKeyboardButton(x.replace("_", " "), callback_data=f"xc:{i}") for i, x in enumerate(l3m[d["l2x"]])]
        await say(update, "التفصيل:", smarkup(pairs(btns)))
    elif state == S_NOTE:
        await say(update, "ملاحظة تتكتب في البيان (اختياري):",
                  smarkup([[InlineKeyboardButton("تخطي", callback_data="xn:skip")]]))
    elif state == S_CTYPE:
        if d["flow"] == "adv":
            labels, title = ["سلفة", "رد سلفة"], "أهلاً ❀ سلفة ولا رد سلفة؟"
        else:
            labels, title = [CUSTODY_LABELS[c] for c in CUSTODY_TYPES] + ["رد عهدة", "تسوية عهدة"], "أهلاً ❀ نوع العهدة:"
        btns = [InlineKeyboardButton(x, callback_data=f"ct:{i}") for i, x in enumerate(labels)]
        await say(update, title, smarkup(pairs(btns)))
    elif state == S_EMP:
        await say(update, "اكتب جزء من اسم الموظف وهظهرلك الأسماء:", smarkup([]))
    elif state == S_AMOUNT:
        await say(update, "اكتب المبلغ (أرقام بس):", smarkup([]))
    elif state == S_KIND:
        ask = {"xfer": "التحويل من فين؟" if d["phase"] == "from" else "التحويل لفين؟",
               "cret": "الفلوس اترجعت فين؟", "aret": "الفلوس اترجعت فين؟", "inc": "الإيراد اتحصّل فين؟"}.get(d["flow"], "اتدفع من فين؟")
        await say(update, ask, smarkup([[
            InlineKeyboardButton("خزينة", callback_data="sk:cash"),
            InlineKeyboardButton("بنك", callback_data="sk:bank"),
            InlineKeyboardButton("فودافون كاش", callback_data="sk:wallet"),
        ]]))
    elif state == S_METHOD:
        items = L[d["kind"]]
        labels = [wallet_label(x) for x in items] if d["kind"] == "wallet" else items
        text = "اختار:"
        if d["kind"] == "wallet":
            lines = [f"{x}\n({WALLET_NUMBERS[x]})" for x in labels if x in WALLET_NUMBERS]
            if lines:
                text += "\n\n" + "\n\n".join(lines)
        btns = [InlineKeyboardButton(t, callback_data=f"sm:{i}") for i, t in enumerate(labels)]
        await say(update, text, smarkup([[b] for b in btns]))
    elif state == S_OTHER:
        await say(update, "اكتب اسم البنك:", smarkup([]))
    elif state == S_ATT:
        await say(update, ATT_PROMPT, smarkup([[InlineKeyboardButton("تخطي", callback_data="at:skip")]]))
    elif state == S_VOUCH:
        await say(update, "اكتب رقم الوصل (لازم):", smarkup([]))
    elif state == S_DATE:
        await say(update, "التاريخ؟ اضغط اليوم أو اكتبه بالشكل ده: 25/10/2026",
                  smarkup([[InlineKeyboardButton("اليوم", callback_data="sd:today")]]))
    elif state == S_CONFIRM:
        await say(update, sup_summary(d), smarkup([[
            InlineKeyboardButton("تأكيد وتسجيل", callback_data="sgo"),
            InlineKeyboardButton("إلغاء", callback_data="scancel"),
        ]]))


def pay_label(method, other):
    return f"{method} ({other})" if other else method


def _sup_desc(d):
    f = d["flow"]
    if f == "buy":
        item = (d.get("aitem") or d["aclass"]).replace("_", " ")
        return f"شراء أصل {item}" + (f" من {d['supplier']}" if d.get("kind") == "credit" else "")
    if f == "sup":
        return f"دفعة توريد {d['supplier']}" if d["stype"] == "الدفعات" else f"اثبات خصم {d['supplier']}"
    if f == "cret":
        return f"رد عهدة مؤقتة من {d['emp'].strip()}"
    if f == "aret":
        return f"رد سلفة من {d['emp'].strip()}"
    if f == "xfer":
        return f"تحويل من {pay_label(*d['src'])} إلى {pay_label(d['method'], d['other_bank'])}"
    if f == "sale":
        return f"قيمة المبيعات العميل {d['client']} تبع {d['seller']}"
    if f == "inc":
        item = (d.get("l4x") or d["l3x"] or d["l2x"]).replace("_", " ")
        return f"إيراد {item}" + (f" - {d['note']}" if d.get("note") else "")
    if f == "exp":
        item = (d.get("l4x") or d["l3x"] or d["l2x"]).replace("_", " ")
        base = f"تسوية عهدة {d['emp'].strip()} - {item}" if d.get("kind") == "custody" else f"مصروف {item}"
        return base + (f" - {d['note']}" if d.get("note") else "")
    emp = d["emp"].strip()
    if f == "cus":
        return f"منصرف {CUSTODY_LABELS[d['ctype']]} {emp}"
    return f"منصرف سلفة {emp}"


def sup_desc(d):
    """بيان القيد. لو جاي من تحويل أو طلب خزنة بنضيف البيان الأصلي في الآخر (المصروفات والإيرادات بتضيفه لوحدها)."""
    base = _sup_desc(d)
    if d.get("note") and d.get("flow") not in ("exp", "inc"):
        base += f" - {d['note']}"
    return base


def sup_summary(d):
    f = d["flow"]
    bank = f" ({d['other_bank']})" if d["other_bank"] else ""
    if f == "sup":
        head = f"المورد: {d['supplier']}\nالنوع: {d['stype']}\n"
    elif f in ("cret", "aret"):
        head = f"الموظف: {d['emp'].strip()}\nالنوع: " + ("رد عهدة مؤقتة" if f == "cret" else "رد سلفة") + "\n"
    elif f == "xfer":
        head = f"من: {pay_label(*d['src'])}\nإلى: {pay_label(d['method'], d['other_bank'])}\n"
    elif f == "buy":
        head = f"الأصل: {d['aclass'].replace('_', ' ')}" + (f" > {d['aitem']}" if d.get("aitem") else "") + "\n"
        if d.get("kind") == "credit":
            head += f"المورد: {d['supplier']}\n"
    elif f == "sale":
        head = f"العميل: {d['client']}\nالبياع: {d['seller']}\nالمشروع: {d['project']}\nالنوع: قيمة المبيعات\n"
    elif f == "inc":
        head = f"الإيراد: {d['l1x']} > {d['l2x']}" + (f" > {d['l3x']}" if d["l3x"] else "") + (f" > {d['l4x']}" if d.get("l4x") else "") + "\n"
    elif f == "exp":
        head = f"المصروف: {d['l1x']} > {d['l2x']}" + (f" > {d['l3x']}" if d["l3x"] else "") + (f" > {d['l4x']}" if d.get("l4x") else "") + "\n"
    elif f == "cus":
        head = f"الموظف: {d['emp'].strip()}\nالنوع: {CUSTODY_LABELS[d['ctype']]}\n"
    else:
        head = f"الموظف: {d['emp'].strip()}\nالنوع: سلفة\n"
    return (
        "راجع القيد:\n" + head
        + f"المبلغ: {d['amount']:,.2f}\n"
        + (f"الدفع: {d['method']}{bank}\n" if d["method"] and f != "xfer" else "")
        + (f"رقم الوصل: {d['voucher']}\n" if d.get("voucher") else "")
        + ("المرفق: ✅\n" if d.get("att") else "")
        + f"التاريخ: {d['date']}\n"
        f"البيان: {sup_desc(d)}\n\n"
        "متأكد إن البيانات دي صح؟"
    )


async def sgo(update, context, state):
    context.user_data.setdefault("shist", []).append(state)
    await sprompt(update, context, state)
    return state


async def sgo_back(update, context):
    hist = context.user_data.get("shist")
    if not hist or "sd" not in context.user_data:
        await update.callback_query.answer()
        return None
    if len(hist) <= 1:              # أول خطوة: رجوع للقايمة الرئيسية
        tq = (context.user_data.get("sd") or {}).get("tq")
        cx = (context.user_data.get("sd") or {}).get("cx")
        context.user_data.clear()
        if tq and await tq_back_to_menu(update, tq):
            return ConversationHandler.END
        if cx and await cx_back_menu(update, cx):
            return ConversationHandler.END
        await say(update, "تفتح إيه؟", menu_markup())
        return ConversationHandler.END
    hist.pop()
    sd = context.user_data["sd"]
    if sd.get("flow") == "xfer":
        sd["phase"] = "to" if hist.count(S_KIND) >= 2 else "from"
    await sprompt(update, context, hist[-1])
    return hist[-1]


async def _begin(update, context, flow, first):
    await ensure_roles()
    uid = update.effective_user.id
    if uid not in ADMINS:
        if update.callback_query:
            await update.callback_query.answer("للمحاسبين بس", show_alert=True)
        else:
            await update.message.reply_text("الأمر ده للمحاسبين بس.")
        return ConversationHandler.END
    context.user_data.clear()
    context.user_data["sL"] = await run(read_lists)
    context.user_data["sd"] = {"uid": uid, "flow": flow, "method": "", "other_bank": "", "phase": "from"}
    context.user_data["shist"] = []
    return await sgo(update, context, first)


async def sup_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await owner_form_open(update, context, "sup")


async def cus_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await owner_form_open(update, context, "cus")


async def adv_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await owner_form_open(update, context, "adv")


async def cus_type(update, context):
    d = context.user_data["sd"]
    i = int(update.callback_query.data.split(":")[1])
    if d["flow"] == "adv":
        d["flow"] = "adv" if i == 0 else "aret"
        return await sgo(update, context, S_EMP)
    if i == len(CUSTODY_TYPES):          # رد عهدة
        d["flow"] = "cret"
        return await sgo(update, context, S_EMP)
    if i == len(CUSTODY_TYPES) + 1:      # تسوية عهدة: قايمة فرعية
        if d.get("preset"):
            await update.callback_query.answer("تسوية العهدة مش من هنا. اختار نوع تاني.", show_alert=True)
            return S_CTYPE
        return await sgo(update, context, S_STL)
    ctype = CUSTODY_TYPES[i]
    if ctype not in CUS_DONE:
        await say(update, f"قيد {CUSTODY_LABELS[ctype]} لسه مش متحدد، فماتسجلش حاجة. ارجع لمسؤول الحسابات. 🚫")
        context.user_data.clear()
        return ConversationHandler.END
    d["ctype"] = ctype
    return await sgo(update, context, S_EMP)


async def stl_pick(update, context):
    d = context.user_data["sd"]
    i = int(update.callback_query.data.split(":")[1])
    if i == 0:
        await say(update, "قيد تسوية عهدة م/ محمد جمعة لسه مش متحدد، فماتسجلش حاجة. ارجع لمسؤول الحسابات. 🚫")
        context.user_data.clear()
        return ConversationHandler.END
    if i == 1:
        context.user_data.clear()
        await inbox_run(update, context)
        return ConversationHandler.END
    d["flow"], d["kind"], d["settle"] = "exp", "custody", True      # موظف آخر: الموظف ثم بند المصروف
    return await sgo(update, context, S_EMP)


async def emp_search(update, context):
    q = norm(update.message.text)
    matches = [e for e in EMPLOYEES if q in norm(e)][:10]
    if not matches:
        await update.message.reply_text("مفيش موظف بالاسم ده. جرب جزء تاني من الاسم:")
        return S_EMP
    context.user_data["sd"]["emp_matches"] = matches
    btns = [[InlineKeyboardButton(e.strip(), callback_data=f"se:{i}")] for i, e in enumerate(matches)]
    await update.message.reply_text("اختار الموظف:", reply_markup=smarkup(btns))
    return S_EMP


async def emp_pick(update, context):
    d = context.user_data["sd"]
    d["emp"] = d["emp_matches"][int(update.callback_query.data.split(":")[1])]
    if d.get("settle"):
        d["method"] = f"عهدة {d['emp'].strip()}"
        return await sgo(update, context, S_EL1)
    if d.get("preset"):
        return await sgo(update, context, preset_next(d))
    return await sgo(update, context, S_AMOUNT)


async def sup_pick(update, context):
    i = int(update.callback_query.data.split(":")[1])
    context.user_data["sd"]["supplier"] = SUPPLIERS[i]
    return await sgo(update, context, S_TYPE)


async def sup_type(update, context):
    i = int(update.callback_query.data.split(":")[1])
    stype = SUPPLIER_TYPES[i]
    if stype not in SUP_DONE:
        await say(update, f"قيد {stype} لسه مش متحدد، فماتسجلش حاجة. ارجع لمسؤول الحسابات. 🚫")
        context.user_data.clear()
        return ConversationHandler.END
    context.user_data["sd"]["stype"] = stype
    d = context.user_data["sd"]
    if d.get("preset"):
        return await sgo(update, context, preset_next(d) if stype == SUPPLIER_TYPES[0] else S_CONFIRM)
    return await sgo(update, context, S_AMOUNT)


async def sup_amount(update, context):
    t = update.message.text.translate(AR_DIGITS).replace(",", "").strip()
    try:
        amount = float(t)
        assert amount > 0
    except Exception:
        await update.message.reply_text("المبلغ غلط. اكتب أرقام بس، مثلًا 50000:")
        return S_AMOUNT
    d = context.user_data["sd"]
    d["amount"] = amount
    d["method"], d["other_bank"] = "", ""
    needs_source = d["flow"] not in ("sup", "sale") or (d["flow"] == "sup" and d["stype"] == SUPPLIER_TYPES[0])
    if d.get("settle"):
        needs_source = False
    return await sgo(update, context, S_KIND if needs_source else S_DATE)


async def exp_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await owner_form_open(update, context, "exp")


async def buy_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await owner_form_open(update, context, "buy")


async def exp_l1(update, context):
    d = context.user_data["sd"]
    d["l1x"] = tree_of(d)[0][int(update.callback_query.data.split(":")[1])]
    return await sgo(update, context, S_EL2)


async def exp_l2(update, context):
    d = context.user_data["sd"]
    _, tree, l3m = tree_of(d)
    d["l2x"] = tree[d["l1x"]][int(update.callback_query.data.split(":")[1])]
    d["l3x"] = ""
    return await sgo(update, context, S_EL3 if d["l2x"] in l3m else (preset_next(d) if d.get("preset") else S_AMOUNT))


async def exp_l3(update, context):
    d = context.user_data["sd"]
    d["l3x"] = tree_of(d)[2][d["l2x"]][int(update.callback_query.data.split(":")[1])]
    return await sgo(update, context, preset_next(d) if d.get("preset") else S_AMOUNT)


async def exp_note(update, context):
    d = context.user_data["sd"]
    d["note"] = "" if update.callback_query else update.message.text.strip()
    return await sgo(update, context, S_ATT)


async def sale_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await owner_form_open(update, context, "sale")


async def sale_type(update, context):
    stype = SALE_TYPES[int(update.callback_query.data.split(":")[1])]
    if stype != "قيمة المبيعات":
        await say(update, f"قيد {stype} لسه مش متحدد، فماتسجلش حاجة. ارجع لمسؤول الحسابات. 🚫")
        context.user_data.clear()
        return ConversationHandler.END
    return await sgo(update, context, S_SELLER)


async def cret_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await owner_form_open(update, context, "cus", {"ctype": "رد عهدة"})


async def aret_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await owner_form_open(update, context, "adv", {"atype": "رد سلفة"})


async def xfer_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await owner_form_open(update, context, "xfer")


async def _after_method(update, context):
    d = context.user_data["sd"]
    if d["flow"] == "xfer" and d["phase"] == "from":
        d["src"] = (d["method"], d["other_bank"])
        d["src_cash"] = d.get("kind") == "cash"
        d["phase"] = "to"
        if d.get("dst_preset"):      # استلام: الحساب المستلم فيه جاي من التحويل
            d["kind"], d["method"], d["other_bank"] = d["dst_preset"]
            return await sgo(update, context, S_VOUCH if ((d["kind"] == "cash" or d["src_cash"]) and not d.get("voucher")) else S_CONFIRM)
        d["method"], d["other_bank"] = "", ""
        return await sgo(update, context, S_KIND)
    if (d.get("kind") == "cash" or d.get("src_cash")) and not d.get("voucher"):
        return await sgo(update, context, S_VOUCH)      # رقم الوصل لازم لأي حركة على الخزنة
    return await sgo(update, context, S_CONFIRM if d.get("preset") else S_DATE)


async def sale_seller(update, context):
    L = context.user_data["sL"]
    context.user_data["sd"]["seller"] = L["sellers"][int(update.callback_query.data.split(":")[1])]
    return await sgo(update, context, S_CLIENT)


async def sale_client(update, context):
    name = update.message.text.strip()
    if not name:
        await update.message.reply_text("اكتب اسم العميل:")
        return S_CLIENT
    context.user_data["sd"]["client"] = name
    return await sgo(update, context, S_PROJECT)


async def sale_project(update, context):
    p = update.message.text.translate(AR_DIGITS).strip()
    if not p:
        await update.message.reply_text("اكتب رقم المشروع:")
        return S_PROJECT
    context.user_data["sd"]["project"] = p
    return await sgo(update, context, S_AMOUNT)


async def sup_kind(update, context):
    context.user_data["sd"].pop("voucher", None)
    kind = update.callback_query.data.split(":")[1]
    context.user_data["sd"]["kind"] = kind
    return await sgo(update, context, S_EMP if kind == "custody" else S_METHOD)


async def sup_method(update, context):
    d = context.user_data["sd"]
    L = context.user_data["sL"]
    d["method"] = L[d["kind"]][int(update.callback_query.data.split(":")[1])]
    d["other_bank"] = ""
    if d["method"].startswith("أخرى"):
        return await sgo(update, context, S_OTHER)
    return await _after_method(update, context)


async def sup_other(update, context):
    context.user_data["sd"]["other_bank"] = update.message.text.strip()
    return await _after_method(update, context)


def credit_side(L, method, other):
    if method == GOMAA_WALLET:
        return (L["l1"], CUSTODY_L2, CUSTODY_L3, CUSTODY_PERSON)
    if method.startswith("أخرى"):
        return (L["l1"], L["l2_bank"], other, "")
    if method in L["bank_l3"]:
        return (L["l1"], L["l2_bank"], L["bank_l3"][method], "")
    return (L["l1"], L["l2_cash"], L["cash_l3"].get(method, method), "")


def sup_post(d, L, user):
    amount, desc, f = d["amount"], sup_desc(d), d["flow"]
    row = lambda l1, l2, l3, l4, dr, cr: [user, d["date"], d.get("voucher", ""), desc, l1, l2, l3, l4, 0, "", "", "", "", "", "", dr, cr]
    if f in ("cret", "aret", "xfer"):
        dst = credit_side(L, d["method"], d["other_bank"])
        if f == "xfer":
            src = credit_side(L, *d["src"])
        else:
            src = (CUS_L1, CUS_L2, "العهد_المؤقتة" if f == "cret" else ADV_L3, d["emp"])
        lines = [row(*dst, amount, ""), row(*src, "", amount)]
    elif f == "sale":
        cc = [L["cc"], d["seller"], d["client"], d["project"]]
        mk = lambda l1, l2, l3, l4, dr, cr: [user, d["date"], d.get("voucher", ""), desc, l1, l2, l3, l4, 0, "", "", *cc, dr, cr]
        lines = [mk(L["l1"], L["l2_client"], d["client"], "قيمة المبيعات", amount, ""),
                 mk(SUP_L1, "الإيرادات_غير_المحققة", "قيمة المبيعات", "", "", amount)]
    elif f == "buy":      # شراء أصل: مدين الأصل، دائن الخزينة/البنك/المحفظة أو المورد (آجل)
        asset = row(ASSET_L1, ASSET_L2, d["aclass"], d.get("aitem", ""), amount, "")
        if d.get("kind") == "credit":
            other = row(SUP_L1, SUP_L2, d["supplier"], "", "", amount)
        else:
            other = row(*credit_side(L, d["method"], d["other_bank"]), "", amount)
        lines = [asset, other]
    elif f == "sup" and d["stype"] == "خصومات":
        lines = [row(SUP_L1, SUP_L2, d["supplier"], "خصومات", amount, ""), row(DISC_L1, DISC_L2, "", "", "", amount)]
    else:
        if d.get("kind") == "custody":
            c = (CUS_L1, CUS_L2, "العهد_المؤقتة", d["emp"])
        else:
            c = credit_side(L, d["method"], d["other_bank"])
        if f == "exp":      # مصروف
            debit = row(d["l1x"], d["l2x"], d["l3x"], d.get("l4x", ""), amount, "")
        elif f == "sup":      # دفعة مورد
            debit = row(SUP_L1, SUP_L2, d["supplier"], "الدفعات", amount, "")
        elif f == "inc":     # إيراد: مدين الخزنة/البنك، دائن حساب الإيراد
            debit = None
        elif f == "cus":    # صرف عهدة
            debit = row(CUS_L1, CUS_L2, d["ctype"], d["emp"], amount, "")
        else:               # سلفة
            debit = row(CUS_L1, CUS_L2, ADV_L3, d["emp"], amount, "")
        if f == "inc":
            lines = [row(*c, amount, ""), row(d["l1x"], d["l2x"], d["l3x"], d.get("l4x", ""), "", amount)]
        else:
            lines = [debit, row(*c, "", amount)]
    gl_append(lines)
    _GL_CACHE["t"] = 0
    return lines


async def sup_date(update, context):
    d = context.user_data["sd"]
    if update.callback_query:
        d["date"] = datetime.now().strftime("%Y-%m-%d")
    else:
        t = update.message.text.translate(AR_DIGITS).strip()
        try:
            d["date"] = datetime.strptime(t, "%d/%m/%Y").strftime("%Y-%m-%d")
        except ValueError:
            await update.message.reply_text("التاريخ غلط. اكتبه كده: 25/10/2026")
            return S_DATE
    if d.get("tq") or d.get("cx"):      # البيان والمرفق جايين من التحويل
        return await sgo(update, context, S_CONFIRM)
    return await sgo(update, context, S_NOTE if d["flow"] in ("exp", "inc") else S_ATT)


async def sup_confirm(update, context):
    d = context.user_data["sd"]
    user = ADMIN_USERS.get(d["uid"], ACCOUNTANTS[0])
    if d.get("att"):
        d["att_path"] = await save_ref(context, sd_cat(d), d.get("submitter") or user, att_ref(d["att"]), sup_desc(d), d.get("date", ""))
    lines = await run(sup_post, d, context.user_data["sL"], user)
    try:
        await run(log_actions, d, context.user_data["sL"], user, lines)
    except Exception as e:      # فشل التسجيل في شيت المتابعة ما يوقفش القيد
        logging.warning("log_actions failed: %s", e)
    msg = "تم التسجيل في GL_Entries ✅\n\n" + sup_summary(d).split("\n\nمتأكد")[0].replace("راجع القيد:", "القيد:")
    if d.get("att"):
        msg += "\n\nالمرفق اتحفظ ✅" if d.get("att_path") else "\n\n⚠️ القيد اتسجّل بس المرفق ماتحفظش."
    await say(update, msg)
    if d.get("tq"):          # تحويل جاي من م/ أحمد أو م/ جمعه: نقفله ونبلّغ صاحبه
        try:
            ws, row, rec = await run(tq_find, d["tq"])
            if row:
                await run(ws.update, f"J{row}:K{row}", [["اتسجّل", d.get("att_path", "")]])
                if rec["tg"].isdigit():
                    await context.bot.send_message(int(rec["tg"]), f"التحويل ({d['amount']:,.2f}) اتأكد واتسجّل ✅")
        except Exception as e:
            logging.warning("transfer close failed: %s", e)
    if d.get("cx"):          # طلب أمين الخزنة: نقفله ونبلّغه
        await cx_close(context, d, lines)
    if d.get("tq"):          # نمسح رسالة الطلب والصورة ونسيب البيان بس
        await msg_purge(context.bot, f"t{d['tq']}", update)
    if d.get("cx"):
        await msg_purge(context.bot, f"c{d['cx']}", update)
    context.user_data.clear()
    return ConversationHandler.END


async def sup_cancel(update, context):
    context.user_data.clear()
    await say(update, "اتلغى. ابعت /supplier لو عايز تبدأ من جديد.")
    return ConversationHandler.END


# ---------------- التقارير (من تاب GL_Entries) ----------------
_GL_CACHE = {"t": 0, "rows": []}


def read_gl():
    """بيقرا اليومية من GL_Entries (بيحتفظ بنسخة 60 ثانية). عمود 5=L2 ... 15=مدين 16=دائن."""
    import time
    if time.time() - _GL_CACHE["t"] > 60:
        rows = [r for r in sheet.worksheet("GL_Entries").get_all_values()[1:] if any(str(c).strip() for c in r)]
        _GL_CACHE.update(t=time.time(), rows=[r + [""] * (17 - len(r)) for r in rows])
    return _GL_CACHE["rows"]


def num(x):
    try:
        return float(str(x).replace(",", "").strip() or 0)
    except ValueError:
        return 0.0


def parse_date(x):
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%b-%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(str(x).strip(), fmt)
        except ValueError:
            pass
    return None


def balances(rows, l2, key=6, credit_normal=False, l3=None):
    """رصيد كل حساب: مدين - دائن (أو العكس للحسابات الدائنة). key=6 المستوى التالت، 7 المستوى الرابع."""
    out = {}
    for r in rows:
        if r[5].strip() != l2 or (l3 and r[6].strip() != l3):
            continue
        k = r[key].strip()
        v = num(r[15]) - num(r[16])
        out[k] = out.get(k, 0) + (-v if credit_normal else v)
    return {k: v for k, v in out.items() if abs(v) >= 0.5}


def money(v):
    return f"{v:,.0f}"


LINE = "━━━━━━━━━━━━━━━"


def esc(s):
    import html
    return html.escape(str(s), quote=False)


def cut_text(text, limit=3900):
    """بيقص الرسالة عند آخر سطر كامل (كل سطر تاجاته كاملة جواه) عشان تليجرام ما يرفضش الـ HTML."""
    if len(text) <= limit:
        return text
    return text[:limit].rsplit("\n", 1)[0] + "\n\n… (باقي التقرير اتقص لطول الرسالة)"


def rep_head(icon, title, sub=""):
    return f"{icon} <b>{esc(title)}</b>" + (f"\n<i>{esc(sub)}</i>" if sub else "") + f"\n{LINE}"


def rep_item(name, v, unit="ج"):
    return f"▫️ {esc((name or 'بدون اسم').replace('_', ' '))}: <b>{money(v)}</b> {unit}"


def listing(title, d, only_positive=True, unit="ج", icon="📋", sub=""):
    items = sorted(((k or "بدون اسم", v) for k, v in d.items() if (v > 0 or not only_positive)), key=lambda kv: -kv[1])
    lines = [rep_item(k, v, unit) for k, v in items[:40]] or ["▫️ مفيش"]
    more = f"\n... و{len(items) - 40} غيرهم" if len(items) > 40 else ""
    total = sum(v for _, v in items)
    return ("\n".join([rep_head(icon, title, sub), ""] + lines) + more
            + f"\n\n{LINE}\n🔢 العدد: <b>{len(items)}</b>\n✅ <b>الإجمالي: {money(total)} {unit}</b>")


def report_liquidity(rows):
    cash = balances(rows, "النقدية_بالخزينة")
    bank_all = balances(rows, "النقدية_بالبنوك")
    wallets, banks = {}, {}
    for k, v in bank_all.items():
        (wallets if is_wallet(k) else banks)[k] = v
    backup = {k: v for k, v in wallets.items() if "احتياط" in norm(k)}
    main_w = {k: v for k, v in wallets.items() if k not in backup}

    def block(icon, title, d):
        body = "\n".join(f"      ▫️ {esc(k.replace('_', ' '))}: {money(v)}" for k, v in sorted(d.items())) or "      ▫️ مفيش"
        return f"{icon} <b>{title}</b>: <b>{money(sum(d.values()))}</b> ج\n{body}"

    total = sum(cash.values()) + sum(bank_all.values())
    return "\n".join([rep_head("💰", "الموقف النقدي"), "",
                      block("🏦", "إجمالي الخزن", cash), "",
                      block("🏛", "إجمالي البنوك", banks), "",
                      block("📱", "إجمالي فودافون كاش", main_w), "",
                      block("🛡", "إجمالي المحفظة الاحتياطية", backup), "",
                      LINE, f"✅ <b>إجمالي السيولة: {money(total)} ج</b>"])


def month_rows(rows):
    now = datetime.now()
    out = []
    for r in rows:
        dt = parse_date(r[1])
        if dt and dt.year == now.year and dt.month == now.month:
            out.append(r)
    return out


def report_expenses_month(rows):
    d = {}
    for r in month_rows(rows):
        if r[4].strip() in EXP_L1:
            d[r[5].strip()] = d.get(r[5].strip(), 0) + num(r[15]) - num(r[16])
    return listing("مصروفات الشهر الحالي", d, icon="📉", sub="حسب البند")


def report_sales_month(rows):
    total, cnt = 0.0, 0
    for r in month_rows(rows):
        if r[5].strip() == "الإيرادات_غير_المحققة" and r[6].strip() == "قيمة المبيعات":
            total += num(r[16]) - num(r[15])
            cnt += 1
    return "\n".join([rep_head("🧾", "مبيعات الشهر الحالي", "قيمة المبيعات المسجلة"), "",
                      f"🔢 عدد العمليات: <b>{cnt}</b>", "", LINE, f"✅ <b>الإجمالي: {money(total)} ج</b>"])


REPORTS = {
    "liq": lambda r: report_liquidity(r),
    "cli": lambda r: listing("العملاء", balances(r, "العملاء"), icon="👥", sub="المستحق لنا"),
    "sup": lambda r: listing("الموردين", balances(r, "الموردين", credit_normal=True), icon="🏭", sub="المستحق علينا"),
    "exp": report_expenses_month,
    "sal": report_sales_month,
    "cus": lambda r: listing("العهد", cust_balances(r, ("العهد_المؤقتة", "العهد_المستديمة")), icon="👷", sub="اللي عليهم عهد"),
    "adv": lambda r: listing("السلف", cust_balances(r, ("سلف_العاملين",)), icon="💵", sub="اللي عليهم سلف"),
}
CEO_REPORTS = ["liq", "cli", "sup", "exp", "sal"]
REPORT_NAMES = {"liq": "الموقف النقدي", "cli": "العملاء", "sup": "الموردين", "exp": "مصروفات الشهر",
                "sal": "مبيعات الشهر", "cus": "العهد", "adv": "السلف"}


def cust_balances(rows, l3s):
    out = {}
    for r in rows:
        if r[5].strip() == "مدينون_وارصده_مدينة" and r[6].strip() in l3s:
            k = r[7].strip()
            out[k] = out.get(k, 0) + num(r[15]) - num(r[16])
    return {k: v for k, v in out.items() if abs(v) >= 0.5}


def report_menu(uid):
    """الموقف النقدي لوحده في سطر، العملاء جنب الموردين، المصروفات جنب المبيعات."""
    mk = lambda k: InlineKeyboardButton(REPORT_NAMES[k], callback_data=f"rp:{k}")
    rows = [[mk("liq")], [mk("cli"), mk("sup")], [mk("exp"), mk("sal")]]
    if not (uid in CEO_IDS and uid not in ADMINS):
        rows.append([mk("cus"), mk("adv")])
    return InlineKeyboardMarkup(rows)


def greeting():
    try:
        from zoneinfo import ZoneInfo
        hour = datetime.now(ZoneInfo("Africa/Cairo")).hour
    except Exception:
        hour = datetime.now().hour
    return "صباح الخير" if hour < 12 else "مساء الخير"


async def report_cb(update, context):
    await ensure_roles()
    q = update.callback_query
    uid = q.from_user.id
    key = q.data.split(":")[1]
    is_ceo = uid in CEO_IDS and uid not in ADMINS
    if uid not in ADMINS and uid not in CEO_IDS:
        await q.answer("غير مسموح", show_alert=True)
        return
    await q.answer()
    if key == "home":
        if MINIAPP_URL:
            await q.edit_message_text("تمام 👍 اختار من الأزرار اللي تحت.")
            return
        await q.edit_message_text("حضرتك تختار إيه؟", reply_markup=ceo_menu(uid))
        return
    if key in ("menu", "back"):
        if is_ceo:
            await q.edit_message_text("حضرتك تختار أنهي تقرير؟", reply_markup=ceo_reports_markup(uid))
        else:
            await q.edit_message_text("اختار التقرير:", reply_markup=report_menu(uid))
        return
    if key not in REPORTS or (is_ceo and key not in CEO_REPORTS):
        await q.answer("غير مسموح", show_alert=True)
        return
    rows = await run(read_gl)
    text = REPORTS[key](rows)
    back = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ رجوع", callback_data="rp:back")]])
    await q.edit_message_text(cut_text(text), reply_markup=back, parse_mode="HTML")


async def open_summary(update, context):
    await ensure_roles()
    q = update.callback_query
    if q.from_user.id not in ADMINS:
        await q.answer("للمحاسبين بس", show_alert=True)
        return
    await q.answer()
    await q.message.reply_text("📊 الملخص (للعرض بس، مفيش حاجة بتتسجل):", reply_markup=report_menu(q.from_user.id))


# ---------------- شيتات متابعة الحركات (زي Pending) ----------------
import re
import uuid

LOG_HEADER = ["وقت التسجيل", "التاريخ", "المستخدم", "النوع", "الطرف", "المبلغ", "الدفع", "البيان", "المرفق"]
CASH_HEADER = ["التاريخ", "رقم الوصل", "البيان", "مصروفات", "إيرادات", "رصيد الخزنة", "النوع", "المستخدم", "وقت التسجيل"]
FLOW_LABELS = {"sup": "موردين", "cus": "صرف عهدة", "adv": "سلفة", "cret": "رد عهدة", "aret": "رد سلفة",
               "exp": "مصروف", "inc": "إيراد", "sale": "مبيعات", "xfer": "تحويل", "buy": "شراء أصل"}


def _tab(title, rows, cols):
    try:
        return sheet.worksheet(title), False
    except gspread.WorksheetNotFound:
        return sheet.add_worksheet(title=title, rows=rows, cols=cols), True


def _append(ws, row):
    """صف واحد أو عدة صفوف مرة واحدة (عشان ما نتخطاش حد الطلبات)."""
    if row and isinstance(row[0], list):
        ws.append_rows(row, value_input_option="USER_ENTERED", table_range="A1")
    else:
        ws.append_row(row, value_input_option="USER_ENTERED", table_range="A1")


def append_log(tab, header, row):
    ws, new = _tab(tab, 1000, len(header))
    if new:
        ws.update("A1", [header])
    _append(ws, row)


def append_emp_log(tab, row, t_taken, t_settled, head_taken, head_settled):
    """شيت العهد / السلف: سجل الحركات + جدول ملخص جنبه (بيتحدث لوحده): الموظف، اللي أخده، التسوية، الفرق، والإجمالي تحت."""
    ws, new = _tab(tab, 1000, 14)
    if new:
        ws.update("A1", [LOG_HEADER])
        ws.update("J1", [["الموظف", head_taken, head_settled, "الفرق"]])
        crit = 'E:E,IF(J2:J="الإجمالي","*",J2:J)'
        taken = f'SUMIFS(F:F,{crit},D:D,"{t_taken}")'
        settled = "+".join(f'SUMIFS(F:F,{crit},D:D,"{t}")' for t in t_settled)
        ws.update("J2", [['={SORT(UNIQUE(FILTER(E2:E,E2:E<>"")));"الإجمالي"}']], value_input_option="USER_ENTERED")
        ws.update("K2", [[f'=ARRAYFORMULA(IF(J2:J="","",{taken}))']], value_input_option="USER_ENTERED")
        ws.update("L2", [[f'=ARRAYFORMULA(IF(J2:J="","",{settled}))']], value_input_option="USER_ENTERED")
        ws.update("M2", [['=ARRAYFORMULA(IF(J2:J="","",K2:K-L2:L))']], value_input_option="USER_ENTERED")
    _append(ws, row)


def cash_sheet_name(l3):
    return "الخزنة الفرعية" if "فرعي" in norm(l3) else "الخزنة الرئيسية"


def cash_row(l3, date, voucher, statement, expense, income, kind, user):
    """كل حركة على الخزنة بتتسجل في شيت الخزنة (الرئيسية أو الفرعية) ورصيد الخزنة بيتحسب بمعادلة."""
    ws, new = _tab(cash_sheet_name(l3), 2000, 9)
    if new:
        ws.update("A1", [CASH_HEADER])
        ws.update("A2", [["", "", "رصيد أول المدة (اكتبه في عمود إيرادات)", 0, 0, "=SUM($E$2:E2)-SUM($D$2:D2)", "", "", ""]],
                  value_input_option="USER_ENTERED")
    res = ws.append_row([date, voucher, statement, expense or "", income or "", "", kind, user,
                         datetime.now().strftime("%Y-%m-%d %H:%M:%S")], value_input_option="USER_ENTERED", table_range="A1")
    row = int(re.search(r"(\d+)$", res["updates"]["updatedRange"]).group(1))
    ws.update_acell(f"F{row}", f"=SUM($E$2:E{row})-SUM($D$2:D{row})")


def log_actions(d, L, user, lines):
    """بيسجل كل حركة في شيت المتابعة بتاعها. ملوش علاقة بالقيود."""
    f = d["flow"]
    settle = bool(d.get("settle"))
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    party = d.get("supplier") or d.get("emp", "").strip() or d.get("client", "") or (d.get("l4x") or d.get("l3x") or d.get("l2x", ""))
    if f == "buy":
        party = (d.get("aitem") or d["aclass"]).replace("_", " ")
    if f == "xfer":
        pay = f"{pay_label(*d['src'])} ← {pay_label(d['method'], d['other_bank'])}"
    else:
        pay = pay_label(d["method"], d["other_bank"]) if d["method"] else ""
    kind = "تسوية عهدة" if settle else FLOW_LABELS[f]
    base = [now, d["date"], user, kind, party, d["amount"], pay, lines[0][3], d.get("att_path", "")]
    if f == "buy":
        append_log("Assets_Log", LOG_HEADER, base)
    if f == "sup":
        append_log("Suppliers_Log", LOG_HEADER, base)
    if f in ("cus", "cret") or settle:
        append_emp_log("Custody_Log", base, "صرف عهدة", ("تسوية عهدة", "رد عهدة"), "العهدة", "التسوية")
    if f in ("adv", "aret"):
        append_emp_log("Advances_Log", base, "سلفة", ("رد سلفة",), "السلفة", "المسترد")
    if f == "exp":
        append_log("Expenses_Log", LOG_HEADER, base)
    if f == "inc":
        append_log("Income_Log", LOG_HEADER, base)
    for ln in lines:
        if ln[5] == L["l2_cash"]:
            incoming = ln[15] not in ("", 0, None)
            cash_row(ln[6], d["date"], d.get("voucher", ""), lines[0][3], None if incoming else ln[16],
                     ln[15] if incoming else None, kind, user)


# ---------------- تسويات الانتقالات من شيت الفورم (Google Form) ----------------
# البوت بيقرا الصفوف اللي محمد حسن علّم عليها صح (STL Rev) ومتسجلتش، ولما تبقى 20 بيبعتهم لك مرة واحدة.
SETTLE_SHEET_ID = os.environ.get("SETTLE_SHEET_ID", "1VdYShBlNXceqpjVNzOet0ol4wghR-53a2-J9NXilrsk")
SETTLE_TAB = os.environ.get("SETTLE_TAB", "Form_Responses")
SETTLE_GID = os.environ.get("SETTLE_GID", "996989455")      # رقم التاب من اللينك (gid=...)
# م/ محمد جمعة (اعتماد التسويات): حط رقمه في .env كده GOMAA_ID=123456789  (بيطلع من /myid لما يبعت للبوت)
GOMAA_ID = int(os.environ["GOMAA_ID"]) if os.environ.get("GOMAA_ID", "").strip().isdigit() else 0
SETTLE_EMP = " محمد حسن ياسين حسن" if " محمد حسن ياسين حسن" in EMPLOYEES else next(
    (e for e in EMPLOYEES if "محمد حسن" in e), "محمد حسن")
SETTLE_L1, SETTLE_L2 = "م_البيع_والتوزيع", "انتقالات"          # نوع المصروف دايمًا انتقالات
SETTLE_COLS = {"ts": "Timestamp", "seller": "اسم مسؤول البيع", "date": "التاريخ", "client": "اسم العميل",
               "frm": "جهة التحرك", "to": "جهة الوصول", "details": "تفاصيل الانتقالات", "reason": "سبب الزيارة",
               "type": "نوع المصروف", "amount": "مبلغ المصروف", "ok": "STL Rev"}


# ---------------- الأدوار من تاب Users_IDs ----------------
_ENV_ADMINS, _ENV_CEO, _ENV_GOMAA = dict(ADMIN_USERS), set(CEO_IDS), GOMAA_ID
CASH_MAIN_IDS, CASH_SUB_IDS = set(), set()
OPERATION_IDS = set()      # يوزر operation: بيوصله إشعار أول ما تحصيل بياع يتسجل في القيود
_ROLES_T = 0


def refresh_roles():
    """بيقرا الأدوار من Users_IDs ويحدّث الصلاحيات: محاسب، CEO، SM (م/ محمد جمعة)، أمناء الخزنة."""
    global GOMAA_ID
    admins, ceo, sm, cm, cs, op = dict(_ENV_ADMINS), set(_ENV_CEO), _ENV_GOMAA, set(), set(), set()
    for uid, name, role in read_users():
        if role == "accountant":
            admins[uid] = name
        elif role == "ceo":
            ceo.add(uid)
        elif role == "sm":
            sm = uid
        elif role == "cash_main":
            cm.add(uid)
        elif role == "cash_sub":
            cs.add(uid)
        elif role == "operation":
            op.add(uid)
    ADMIN_USERS.clear(); ADMIN_USERS.update(admins)
    ADMINS.clear(); ADMINS.update(admins)
    CEO_IDS.clear(); CEO_IDS.update(ceo)
    CASH_MAIN_IDS.clear(); CASH_MAIN_IDS.update(cm)
    CASH_SUB_IDS.clear(); CASH_SUB_IDS.update(cs)
    OPERATION_IDS.clear(); OPERATION_IDS.update(op)
    GOMAA_ID = sm


async def ensure_roles():
    global _ROLES_T
    if time.time() - _ROLES_T < 60:
        return
    _ROLES_T = time.time()
    try:
        await run(refresh_roles)
    except Exception as e:
        logging.warning("refresh_roles failed: %s", e)


def parse_settle(vals, done):
    """بيرجّع (الصفوف المعتمدة اللي لسه ما اتسجلتش, عدد الصفوف المعتمدة بنوع مصروف غير انتقالات)."""
    head = [h.strip() for h in vals[0]]
    missing = [v for v in SETTLE_COLS.values() if v not in head]
    if missing:
        raise ValueError(f"أعمدة ناقصة في شيت التسويات: {missing}")
    ix = {k: head.index(v) for k, v in SETTLE_COLS.items()}
    out, other = [], 0
    for r in vals[1:]:
        r = r + [""] * (len(head) - len(r))
        if r[ix["ok"]].strip().upper() != "TRUE" or not r[ix["ts"]].strip():
            continue
        amount = num(r[ix["amount"]])
        key = f"{r[ix['ts']].strip()}|{r[ix['seller']].strip()}|{amount:g}"
        if key in done or amount <= 0:
            continue
        if r[ix["type"]].strip() != SETTLE_L2:
            other += 1
            continue
        dt = None
        for fmt in ("%m/%d/%Y", "%d/%m/%Y", "%Y-%m-%d"):     # الشيت بتاعك بيعرض التاريخ شهر/يوم/سنة
            try:
                dt = datetime.strptime(r[ix["date"]].strip(), fmt)
                break
            except ValueError:
                pass
        if dt is None:
            dt = parse_date(r[ix["ts"]].split(" ")[0]) or datetime.now()
        out.append({"key": key, "seller": r[ix["seller"]].strip(), "date": dt.strftime("%Y-%m-%d"),
                    "client": r[ix["client"]].strip(), "frm": r[ix["frm"]].strip(), "to": r[ix["to"]].strip(),
                    "details": r[ix["details"]].strip(), "reason": r[ix["reason"]].strip(), "amount": amount})
    return out, other


def settle_ws():
    """بيلاقي تاب التسويات بالـgid من اللينك، وبعدين بالاسم، وبعدين بأي تاب فيه عمود STL Rev."""
    ss = gspread.authorize(creds).open_by_key(SETTLE_SHEET_ID)
    tabs = ss.worksheets()
    for w in tabs:
        if str(w.id) == SETTLE_GID:
            return w
    for w in tabs:
        if w.title.strip() == SETTLE_TAB:
            return w
    for w in tabs:
        if "STL Rev" in [h.strip() for h in w.row_values(1)]:
            return w
    raise ValueError("مش لاقي تاب التسويات. التابات الموجودة: " + " | ".join(w.title for w in tabs))


def get_pending():
    ws = settle_ws()
    st, new = _tab("Inbox_State", 2000, 3)
    if new:
        st.update("A1", [["المفتاح", "الحالة", "وقت التسجيل"]])
    done = set(st.col_values(1)[1:])
    return parse_settle(ws.get_all_values(), done)


def mark_state(rows, status):
    st, _ = _tab("Inbox_State", 2000, 3)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    st.append_rows([[r["key"], status, now] for r in rows], value_input_option="RAW", table_range="A1")


def settle_desc(r):
    d = f"تسوية عهدة {SETTLE_EMP.strip()} - انتقالات - {r['seller']} - العميل {r['client']} - {r['reason']}"
    return d + (f" ({r['details']})" if r["details"] else "")


def post_settlements(rows, user):
    """كل صف بيتسجل في قيد لوحده: مدين انتقالات، دائن العهد المؤقتة على محمد حسن."""
    lines, logs = [], []
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for r in rows:
        desc = settle_desc(r)
        mk = lambda l1, l2, l3, l4, dr, cr: [user, r["date"], "", desc, l1, l2, l3, l4, 0, "", "", "", "", r["client"], "", dr, cr]
        lines += [mk(SETTLE_L1, SETTLE_L2, "", "", r["amount"], ""),
                  mk(CUS_L1, CUS_L2, "العهد_المؤقتة", SETTLE_EMP, "", r["amount"])]
        logs.append([now, r["date"], user, "تسوية عهدة", SETTLE_EMP.strip(), r["amount"], "فورم تسويات البياعين", desc])
    gl_append(lines)
    _GL_CACHE["t"] = 0
    mark_state(rows, "posted")           # بنعلّمهم فورًا عشان ما يتسجلوش مرتين
    try:
        append_emp_log("Custody_Log", logs, "صرف عهدة", ("تسوية عهدة", "رد عهدة"), "العهدة", "التسوية")
        append_log("Expenses_Log", LOG_HEADER, logs)
    except Exception as e:
        logging.warning("settlement logs failed: %s", e)
    return len(rows)


def settle_message(batch, other, title):
    """نفس الرسالة بتروح لك ولمحمد جمعة: مجمّعة حسب الموظف، فيها المبلغ والبيان لكل حركة."""
    by = {}
    for r in batch:
        by.setdefault(r["seller"], []).append(r)
    head = f"{title}\n{len(by)} موظفين، {len(batch)} حركة\n"
    tail = (f"\nالإجمالي: {sum(r['amount'] for r in batch):,.0f} ج"
            "\nالقيد لكل حركة: مدين م_البيع_والتوزيع / انتقالات، دائن العهد_المؤقتة / محمد حسن."
            + (f"\n⚠️ فيه {other} صف معتمد بنوع مصروف غير انتقالات (مش داخلين هنا)." if other else ""))
    body, used = "", 0
    for seller, rs in sorted(by.items()):
        block = f"\n👤 {seller}: {len(rs)} حركة، {sum(r['amount'] for r in rs):,.0f} ج\n"
        block += "\n".join(
            f"  • {datetime.strptime(r['date'], '%Y-%m-%d').strftime('%d/%m')} | {r['client']} | {r['reason']}"
            + (f" | {r['details'][:40]}" if r["details"] else "") + f" | {r['amount']:g}" for r in rs)
        if len(head) + len(body) + len(block) + len(tail) > 3800:
            body += f"\n... و{len(batch) - used} حركة كمان (هيتسجلوا معاهم)"
            break
        body += block + "\n"
        used += len(rs)
    return head + body + tail


async def inbox_run(update, context):
    """تسوية عهدة م/ محمد حسن: بيقرا الصفوف المعتمدة (علامة صح) اللي لسه متسجلتش، ويبعتهالك فيها الاختيارات."""
    if update.callback_query:
        await update.callback_query.answer()
    msg = update.effective_message
    if update.effective_user.id != OWNER_ID:
        await msg.reply_text("ده للمسؤول بس.")
        return
    bd = context.application.bot_data
    if bd.get("ib", {}).get("status") == "await_gomaa":
        await msg.reply_text("مستني اعتماد م/ محمد جمعة على الدفعة اللي اتبعتتله.\nلو عايز تلغي الانتظار وتبدأ من جديد ابعت /resetinbox")
        return
    try:
        rows, other = await run(get_pending)
    except Exception as e:
        await msg.reply_text(f"مقدرتش أقرا شيت التسويات ({type(e).__name__}): {e}")
        return
    if not rows:
        await msg.reply_text("مفيش تسويات معتمدة جديدة." + (f"\n⚠️ فيه {other} صف معتمد بنوع مصروف غير انتقالات." if other else ""))
        return
    bid = uuid.uuid4().hex[:6]
    text = settle_message(rows, other, "📥 تسويات انتقالات معتمدة من م/ محمد حسن")
    bd["ib"] = {"id": bid, "rows": rows, "other": other, "status": "new"}
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("تسجيل قيود يومية", callback_data=f"ib:go:{bid}")],
        [InlineKeyboardButton("اعتماد م/ محمد جمعة", callback_data=f"ib:gm:{bid}")],
        [InlineKeyboardButton("رفض", callback_data=f"ib:no:{bid}"),
         InlineKeyboardButton("تجاهل", callback_data=f"ib:skip:{bid}")],
    ])
    await msg.reply_text(text, reply_markup=markup)


async def inbox_cb(update, context):
    await ensure_roles()
    q = update.callback_query
    uid = q.from_user.id
    _, act, bid = q.data.split(":")
    bd = context.application.bot_data
    ib = bd.get("ib")
    if not ib or ib["id"] != bid:
        await q.answer("الدفعة دي خلصت أو قديمة. افتح عهدة > تسوية عهدة > م/ محمد حسن من جديد", show_alert=True)
        return
    if act in ("ga", "gr"):                              # اعتماد/عدم اعتماد: م/ محمد جمعة بس
        allowed = bool(GOMAA_ID) and uid == GOMAA_ID
    else:                                                # باقي الأزرار: إنت بس
        allowed = uid == OWNER_ID
    if not allowed:
        await q.answer("غير مسموح", show_alert=True)
        return
    await q.answer()
    rows = ib["rows"]
    if act == "go":
        bd.pop("ib", None)
        n = await run(post_settlements, rows, ADMIN_USERS.get(OWNER_ID, ACCOUNTANTS[0]))
        await q.edit_message_text(f"تم تسجيل {n} قيد يومية في GL_Entries ✅")
    elif act == "skip":
        bd.pop("ib", None)
        await run(mark_state, rows, "skipped")
        await q.edit_message_text("اتجاهلت الدفعة دي، ومش هتتبعت تاني.")
    elif act == "no":
        bd.pop("ib", None)
        await run(mark_state, rows, "rejected")
        await q.edit_message_text("اترفضت الدفعة دي، ومش هتظهر تاني.")
    elif act == "gm":
        if not GOMAA_ID:
            await q.answer("حط رقم م/ محمد جمعة في .env بالاسم GOMAA_ID الأول", show_alert=True)
            return
        ib["status"] = "await_gomaa"
        text = ((f"{greeting()} م/ محمد جمعة ❀\nصلي على محمد ﷺ ❀\n\n" if GOMAA_ID != OWNER_ID else "")
                + settle_message(rows, ib["other"], "📥 طلب اعتماد تسويات انتقالات (من م/ محمد حسن)"))
        await context.bot.send_message(GOMAA_ID, text, reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("اعتماد", callback_data=f"ib:ga:{bid}"),
            InlineKeyboardButton("عدم اعتماد", callback_data=f"ib:gr:{bid}")]]))
        await q.edit_message_text(q.message.text + "\n\n⏳ اتبعتت لـ م/ محمد جمعة للاعتماد.")
    elif act == "ga":
        ib["status"] = "approved"
        await q.edit_message_text(q.message.text + "\n\n✅ تم الاعتماد.")
        await context.bot.send_message(OWNER_ID, "تم الاعتماد من م/ محمد جمعة ✅\nتحب تسجل القيود؟",
                                       reply_markup=InlineKeyboardMarkup([[
                                           InlineKeyboardButton(f"تأكيد تسجيل {len(rows)} قيد يومية", callback_data=f"ib:go:{bid}")]]))
    elif act == "gr":
        bd.pop("ib", None)
        await q.edit_message_text(q.message.text + "\n\n❌ تم رفض الاعتماد.")
        await context.bot.send_message(OWNER_ID, "تم رفض الاعتماد من م/ محمد جمعة ❌\nالحركات لسه مش متسجلة، وهتظهر تاني لما تفتح تسوية عهدة م/ محمد حسن.")


# ================= م/ أحمد رأفت (CEO): الاعتمادات =================
def ceo_pending_rids():
    out = []
    for holder in CUSTODY_HOLDERS:
        ws, _ = rc_ws(holder)
        for r in ws.get_all_values()[1:]:
            r = r + [""] * (10 - len(r))
            if r[7].strip() == "مستني اعتماد أحمد" and r[8].strip():
                out.append(r[8].strip())
    return out


async def ca_cb(update, context):
    await ensure_roles()
    q = update.callback_query
    if q.from_user.id not in CEO_IDS and q.from_user.id not in ADMINS:
        await q.answer("غير مسموح", show_alert=True)
        return
    await q.answer()
    rids = await run(ceo_pending_rids)
    back = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ رجوع", callback_data="rp:home")]])
    if not rids:
        await q.edit_message_text("مفيش اعتمادات مستنية حضرتك دلوقتي ✅", reply_markup=back)
        return
    await q.edit_message_text(f"فيه {len(rids)} اعتماد مستنيين حضرتك:", reply_markup=back)
    for rid in rids[:10]:
        ws, row, vals = await run(rq_find, rid)
        if not row:
            continue
        d = rq_row_to_d(vals)
        await q.message.reply_text(
            rq_text(d, f"📥 طلب اعتماد تسوية عهدة ({d['seller']})") + "\n\nحضرتك تعتمد؟",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("اعتماد", callback_data=f"rq:ga:{rid}"),
                                                InlineKeyboardButton("عدم اعتماد", callback_data=f"rq:gr:{rid}")]]))


# ================= التحويل (CEO و SM): صورة + بيان بس، والمحاسب بيكمّل الباقي =================
T_KIND, T_BAYAN, T_DATE, T_ATT, T_CONFIRM = range(80, 85)
T_AMOUNT = 85
T_SKIND, T_SMETHOD, T_SOTHER = 86, 87, 88      # (قديمة: مبقاش بيتسأل فيها. المحاسب هو اللي بيحدد التاريخ والحساب والمبلغ)


def tr_who(uid):
    return CEO_NAME if uid in CEO_IDS else "م/ محمد جمعة"


def tr_allowed(uid):
    return uid in CEO_IDS or uid == GOMAA_ID


def tr_kb(rows):
    rows = [list(r) for r in rows] + [[InlineKeyboardButton("⬅️ رجوع", callback_data="tr:back"),
                                       InlineKeyboardButton("إلغاء", callback_data="tr:cancel")]]
    return InlineKeyboardMarkup(rows)


def tr_summary(t):
    return ("حضرتك راجع التحويل:\n"
            f"البيان: {t['bayan']}\n"
            f"المرفق: {'تم ✅' if t.get('att') else 'مفيش'}")


def tr_acc_label(t):
    kinds = {"cash": "خزينة", "bank": "بنك", "wallet": "فودافون كاش"}
    name = t.get("method", "")
    if t.get("other_bank"):
        name += f" ({t['other_bank']})"
    return f"{kinds.get(t.get('acc'), '')} - {name}" if name else "-"


async def tr_prompt(update, context, state):
    t = context.user_data["tr"]
    if state == T_ATT:
        await say(update, "📸 ابعت صورة التحويل، واكتب البيان في الكابشن (الكلام اللي تحت الصورة):", tr_kb([]))
    elif state == T_BAYAN:
        await say(update, "حضرتك اكتب البيان:", tr_kb([]))
    elif state == T_CONFIRM:
        await say(update, tr_summary(t), tr_kb([[InlineKeyboardButton("تأكيد إرسال", callback_data="tc:go")]]))


async def tr_go(update, context, state):
    context.user_data.setdefault("thist", []).append(state)
    await tr_prompt(update, context, state)
    return state


async def tr_start(update, context):
    await ensure_roles()
    q = update.callback_query
    if not tr_allowed(q.from_user.id):
        await q.answer("غير مسموح", show_alert=True)
        return ConversationHandler.END
    await q.answer()
    context.user_data.clear()
    context.user_data["tr"] = {"uid": q.from_user.id, "att": None}
    context.user_data["thist"] = []
    return await tr_go(update, context, T_ATT)


class _TrPhoto(filters.MessageFilter):
    """صورة (من م/ أحمد أو م/ جمعه) ومعاها كابشن = بيان التحويل. بتفتح التأكيد على طول."""

    def filter(self, message):
        u = message.from_user
        cap = (message.caption or "").strip()
        has_img = bool(message.photo) or bool(message.document and (message.document.mime_type or "").startswith("image/"))
        if not u or not has_img or not cap or not tr_allowed(u.id):
            return False
        if re.match(r"^\s*عميل", cap) or cap == ".":      # دي بتاعة التحصيلات / الفورم
            return False
        return u.id not in ADMINS or "تحويل" in cap


async def tr_photo_start(update, context):
    """صورة ومعاها بيان: بنسأله بس يأكد الإرسال."""
    await ensure_roles()
    uid = update.effective_user.id
    if not tr_allowed(uid):
        return ConversationHandler.END
    context.user_data.clear()
    context.user_data["tr"] = {"uid": uid, "att": att_from_msg(update.message),
                               "bayan": (update.message.caption or "").strip()}
    context.user_data["thist"] = []
    await update.message.reply_text("تم استلام الصورة ✅")
    return await tr_go(update, context, T_CONFIRM)


async def tr_bayan(update, context):
    t = update.message.text.strip()
    if not t:
        await update.message.reply_text("حضرتك اكتب البيان:")
        return T_BAYAN
    context.user_data["tr"]["bayan"] = t
    return await tr_go(update, context, T_CONFIRM)


async def tr_att(update, context):
    """بعد زرار تحويل: صورة بكابشن، أو صورة وبعدها البيان، أو بيان لوحده."""
    t = context.user_data["tr"]
    msg = update.message
    a = att_from_msg(msg)
    if a:
        t["att"] = a
        await msg.reply_text("تم استلام الصورة ✅")
        cap = (msg.caption or "").strip()
        if cap:
            t["bayan"] = cap
            return await tr_go(update, context, T_CONFIRM)
        return await tr_go(update, context, T_BAYAN)
    txt = (msg.text or "").strip()
    if txt:
        t["bayan"] = txt
        return await tr_go(update, context, T_CONFIRM)
    await msg.reply_text("ابعت صورة التحويل واكتب البيان تحتها.")
    return T_ATT


async def tr_back(update, context):
    q = update.callback_query
    hist = context.user_data.get("thist")
    if not hist or "tr" not in context.user_data:
        await q.answer()
        return None
    if len(hist) <= 1:              # أول خطوة: رجوع للقايمة الرئيسية بتاعته
        uid = q.from_user.id
        context.user_data.clear()
        await say(update, "حضرتك تختار إيه؟", ceo_menu(uid) if uid in CEO_IDS else gomaa_menu())
        return ConversationHandler.END
    hist.pop()
    await tr_prompt(update, context, hist[-1])
    return hist[-1]


TQ_TAB = "Transfers_Pending"
TQ_HEADER = ["رقم", "وقت التسجيل", "من", "رقم تليجرام", "النوع", "البيان", "المبلغ", "التاريخ", "مرفق", "الحالة", "ملاحظة",
             "نوع الحساب", "الحساب", "بنك آخر"]


def tq_save(t, who):
    """التحويل بيتسجل هنا بحالة "مستني اعتماد". مفيش قيد بيتسجل غير بعد اعتماد المحاسب."""
    ws, new = _tab(TQ_TAB, 1000, len(TQ_HEADER))
    if new:
        ws.update("A1", [TQ_HEADER])
    tid = "T" + uuid.uuid4().hex[:5]
    _append(ws, [tid, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), who, t["uid"],
                 {"in": "استلام", "out": "صرف"}.get(t.get("kind"), ""),
                 _safe_cell(t["bayan"]), t.get("amount", ""), t["date"], att_ref(t.get("att")), "مستني اعتماد", "",
                 t.get("acc", ""), t.get("method", ""), _safe_cell(t.get("other_bank", ""))])
    return tid


def tq_find(tid):
    ws, _ = _tab(TQ_TAB, 1000, len(TQ_HEADER))
    cell = ws.find(tid, in_column=1)
    if not cell:
        return None, None, None
    v = ws.row_values(cell.row, value_render_option="UNFORMATTED_VALUE")
    v = v + [""] * (14 - len(v))
    rec = {"acc": str(v[11]), "method": str(v[12]), "other": str(v[13]), "who": str(v[2]), "tg": str(int(v[3])) if isinstance(v[3], (int, float)) else str(v[3]).strip(),
           "kind": str(v[4]), "bayan": str(v[5]), "amount": num(v[6]), "date": str(v[7]), "att": str(v[8]), "status": str(v[9])}
    if isinstance(v[7], (int, float)):
        from datetime import timedelta
        rec["date"] = (datetime(1899, 12, 30) + timedelta(days=v[7])).strftime("%Y-%m-%d")
    return ws, cell.row, rec


async def tr_confirm(update, context):
    t = context.user_data.get("tr")
    await update.callback_query.answer()
    if not t:
        return ConversationHandler.END
    who = tr_who(t["uid"])
    a = t.get("att")
    t["date"] = datetime.now().strftime("%Y-%m-%d")
    tid = await run(tq_save, t, who)
    text = (f"📥 تحويل جديد من {who}\nالبيان: {t['bayan']}\nالتاريخ: {t['date']}"
            + ("\nالمرفق: ✅ (الصورة تحت)" if a else ""))
    try:
        m_tq = await context.bot.send_message(OWNER_ID, text, reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("اعتماد وتحديد النوع", callback_data=f"tq:{tid}:s")],
            [InlineKeyboardButton("رفض", callback_data=f"tq:{tid}:r")]]))
        msg_track(f"t{tid}", m_tq)
        if a:
            await send_att(context, OWNER_ID, att_ref(a), f"مرفق التحويل ({who})", key=f"t{tid}")
    except Exception as e:
        logging.warning("transfer notify failed: %s", e)
    await say(update, "تم إرسال التحويل لمسؤول الحسابات ✅")
    context.user_data.pop("tr", None)
    return ConversationHandler.END


async def tq_reject(update, context):
    q = update.callback_query
    if q.from_user.id != OWNER_ID:
        await q.answer("غير مسموح", show_alert=True)
        return
    await q.answer()
    ws, row, rec = await run(tq_find, q.data.split(":")[1])
    if not row or rec["status"] != "مستني اعتماد":
        await q.edit_message_text("التحويل ده اتعالج قبل كده.")
        return
    await run(ws.update, f"J{row}", [["مرفوض"]])
    await q.edit_message_text(q.message.text + "\n\n❌ اترفض.")
    await msg_purge(context.bot, f"t{q.data.split(':')[1]}", update)
    if rec["tg"].isdigit():
        try:
            await context.bot.send_message(int(rec["tg"]), f"التحويل ({tq_ref(rec)}) اترفض. اتواصل مع مسؤول الحسابات.")
        except Exception:
            pass


def tq_mark(tid, status):
    ws, row, _ = tq_find(tid)
    if row:
        ws.update(f"J{row}", [[status]])


FULL_OPTS = [("التحصيلات (دفعة عميل)", "pay"), ("موردين", "sup"), ("عهدة", "cus"), ("سلفة", "adv"),
             ("مصروفات", "exp"), ("إيرادات", "inc"), ("تحويل بين الحسابات", "xfer"), ("شراء أصل", "buy")]


def tq_ref(rec):
    """اسم التحويل في رسايل التبليغ: المبلغ لو معروف، وإلا البيان."""
    return f"{rec['amount']:,.2f}" if rec.get("amount") else (rec.get("bayan") or "")


def tq_legacy(rec):
    """تحويل قديم متسجّل بالنوع والمبلغ والحساب (قبل التبسيط)."""
    return bool(rec["acc"] and rec["method"] and rec["amount"] > 0)


def tq_menu(rec, tid):
    """كل الاختيارات مفتوحة قدامك: القيد هيتقفل على إيه؟"""
    if tq_legacy(rec):
        if rec["kind"] == "استلام":
            opts = [("إيرادات", "inc"), ("تحصيل عميل (دفعة عميل)", "pay"), ("عهدة (رد عهدة)", "cus"),
                    ("سلفة (رد سلفة)", "adv"), ("تحويل من حساب تاني", "xfer")]
        else:
            opts = [("مصروفات", "exp"), ("موردين", "sup"), ("عهدة", "cus"), ("سلفة", "adv"), ("تحويل لحساب تاني", "xfer")]
        acc = tr_acc_label({"acc": rec["acc"], "method": rec["method"], "other_bank": rec["other"]})
        text = (f"✅ تحويل ({rec['kind']}) {rec['amount']:,.2f} من {rec['who']}\nالحساب: {acc}\nالبيان: {rec['bayan']}"
                f"\n\nالمبلغ ده يترمي على إيه؟")
    else:
        opts = FULL_OPTS
        text = (f"✅ تحويل من {rec['who']}\nالبيان: {rec['bayan']}\nالتاريخ: {rec['date']}"
                f"\n\nتقفل القيد على إيه؟ (التاريخ والحساب والمبلغ هتكملهم في الخطوات الجاية)")
    btns = [InlineKeyboardButton(n, callback_data=f"tp:{tid}:{k}") for n, k in opts]
    return text, InlineKeyboardMarkup(pairs(btns))


async def tq_back_to_menu(update, tid):
    ws, row, rec = await run(tq_find, tid)
    if not row or rec["status"] != "مستني اعتماد":
        return False
    text, mk = tq_menu(rec, tid)
    await say(update, text, mk)
    return True


async def tq_approve(update, context):
    """المحاسب (إنت) بتعتمد التحويل، وبيظهرلك القايمة كلها: موردين، عملاء، عهدة، سلفة، مصروفات، إيرادات، تحويل بين حسابات."""
    await ensure_roles()
    q = update.callback_query
    if q.from_user.id != OWNER_ID:
        await q.answer("غير مسموح", show_alert=True)
        return
    tid = q.data.split(":")[1]
    ws, row, rec = await run(tq_find, tid)
    if not row or rec["status"] != "مستني اعتماد":
        await q.answer("التحويل ده اتعالج قبل كده", show_alert=True)
        return
    await q.answer()
    text, mk = tq_menu(rec, tid)
    await q.message.reply_text(text, reply_markup=mk)


async def tp_pick(update, context):
    """بعد ما اخترت القيد يتقفل فين: بنكمل خطوات القيد العادي. البيان والمرفق جاهزين من التحويل، والتاريخ والحساب والمبلغ بتكتبهم إنت."""
    await ensure_roles()
    q = update.callback_query
    if q.from_user.id != OWNER_ID:
        await q.answer("غير مسموح", show_alert=True)
        return ConversationHandler.END
    _, tid, typ = q.data.split(":")
    ws, row, rec = await run(tq_find, tid)
    if not row or rec["status"] != "مستني اعتماد":
        await q.answer("التحويل ده اتعالج قبل كده", show_alert=True)
        return ConversationHandler.END
    if typ == "inc" and not await run(income_accounts):
        await q.answer()
        await q.message.reply_text("تاب Income_Accounts في الشيت فاضي. اكتب فيه حسابات الإيرادات وجرّب تاني.")
        return ConversationHandler.END
    await q.answer()
    uid = q.from_user.id
    L = await run(read_lists)
    fid, is_doc = parse_ref(rec["att"])
    legacy = tq_legacy(rec)
    pre = {"tq": tid, "submitter": rec["who"], "note": rec["bayan"], "att": {"id": fid, "doc": is_doc} if fid else None}
    if legacy:          # تحويل قديم: المبلغ والحساب والتاريخ جاهزين
        pre.update(preset=True, amount=rec["amount"], date=rec["date"])
    kind, method, other = rec["acc"], rec["method"], rec["other"]
    if not legacy:               # تحويل جديد (صورة + بيان): فورم كاملة للمحاسب
        meta = {"tq": tid, "submitter": rec["who"], "note": rec["bayan"]}
        vals = {"att": pre["att"], "note": rec["bayan"], "notes": rec["bayan"]}
        return await prefilled_form(update, context, typ, meta, vals)
    context.user_data.clear()
    if typ == "pay":             # تحصيل عميل: نفس فلو دفعة العميل (البياع ← العميل ← نوع الدفعة)
        d = {"uid": uid, **pre, "notes": rec["bayan"]}
        if legacy:
            d.update(kind=kind, method=method, other_bank=other, l3=(L.get(kind + "_l3") or {}).get(method, ""))
        context.user_data["L"] = L
        context.user_data["d"] = d
        context.user_data["hist"] = []
        return await go(update, context, SELLER)
    flows = {"exp": ("exp", S_EL1), "inc": ("inc", S_EL1), "sup": ("sup", S_SUP), "cus": ("cus", S_CTYPE),
             "adv": ("adv", S_CTYPE), "xfer": ("xfer", S_KIND if legacy else S_AMOUNT)}
    flow, first = flows[typ]
    sd = {"uid": uid, "flow": flow, "method": "", "other_bank": "", "phase": "from", **pre}
    if legacy:
        sd.update(kind=kind, method=method, other_bank=other)
        if typ == "xfer":
            if rec["kind"] == "صرف":      # الحساب ده هو المصدر، وإنت بتحدد الحساب اللي راح له
                sd.update(src=(method, other), src_cash=(kind == "cash"), phase="to", kind="", method="", other_bank="")
            else:                          # الحساب ده هو اللي استلم، وإنت بتحدد المصدر
                sd.update(dst_preset=(kind, method, other), phase="from", kind="", method="", other_bank="")
    context.user_data["sL"] = L
    context.user_data["sd"] = sd
    context.user_data["shist"] = []
    return await sgo(update, context, first)


async def tr_cancel(update, context):
    context.user_data.pop("tr", None)
    if update.callback_query:
        await update.callback_query.answer()
    await say(update, "اتلغى. ابعت /start لو حضرتك عايز تبدأ من جديد.")
    return ConversationHandler.END


async def photo_start(update, context):
    """البياع يبعت صورة ومكتوب عليها "عميل": البوت يشتغل كأنه ضغط /start والصورة تتضاف مرفق."""
    return await start_pay(update, context, att=att_from_msg(update.message))


# ---------------- رسايل جت والبوت مقفول: تنبيه + زرار إعادة إرسال ----------------
RESEND_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pending_resend.json")


def _resend_load():
    try:
        with open(RESEND_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def _resend_save(data):
    try:
        with open(RESEND_FILE, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
    except Exception as e:
        logging.warning("couldn't save pending_resend.json: %s", e)


async def notify_missed(app):
    """لما البوت يفتح: نقرأ الرسايل اللي اتبعتت وهو مقفول (تليجرام بيحتفظ بيها 24 ساعة)،
    ونقفلها عشان ماتتنفذش لخبطة من غير سياقها، ونبعت لكل مستخدم تنبيه بزرار إعادة إرسال."""
    try:
        offset, ups = None, []
        for _ in range(50):
            batch = await app.bot.get_updates(offset=offset, limit=100, timeout=0)
            if not batch:
                break
            ups += batch
            offset = batch[-1].update_id + 1
    except Exception as e:
        logging.warning("couldn't read missed updates: %s", e)
        return
    users = {}
    for u in ups:
        msg, cq = u.message, u.callback_query
        user = u.effective_user
        if not user or user.is_bot:
            continue
        if msg:
            if (msg.text or "").strip().lower().startswith("/start"):
                continue
            users.setdefault(user.id, {"chat": msg.chat_id, "updates": []})["updates"].append(u.to_dict())
        elif cq and cq.message:
            users.setdefault(user.id, {"chat": cq.message.chat_id, "updates": []})
    if not users:
        return
    data = _resend_load()
    for uid, info in users.items():
        if info["updates"]:
            old = data.get(str(uid), {}).get("updates", [])
            data[str(uid)] = {"chat": info["chat"], "updates": old + info["updates"]}
    _resend_save(data)
    for uid, info in users.items():
        try:
            if info["updates"]:
                await app.bot.send_message(
                    info["chat"],
                    "⚠️ البوت كان مقفول لما بعتّ، والعملية دي لم يتم تسجيلها.\nاضغط «إعادة إرسال» وهتتبعت تاني.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 إعادة إرسال", callback_data=f"rs:{uid}")]]))
            else:
                await app.bot.send_message(
                    info["chat"],
                    "⚠️ البوت كان مقفول لما ضغطت، والعملية دي لم يتم تسجيلها.\nابعت /start وابدأ العملية من جديد.")
        except Exception as e:
            logging.warning("couldn't notify %s about missed messages: %s", uid, e)


async def resend_cb(update, context):
    """زرار إعادة إرسال: نرجّع رسايل المستخدم اللي ماتسجلتش للبوت بنفس الترتيب."""
    q = update.callback_query
    uid = q.data.split(":")[1]
    if str(q.from_user.id) != uid:
        await q.answer("الزرار ده مش بتاعك", show_alert=True)
        return
    data = _resend_load()
    item = data.pop(uid, None)
    if not item or not item.get("updates"):
        await q.answer()
        await q.edit_message_text("الطلب ده اتبعت قبل كده أو انتهى. ابعت /start وابدأ العملية من جديد.")
        return
    _resend_save(data)
    await q.answer()
    await q.edit_message_text("🔄 اتبعتت تاني. كمّل من هنا.")
    for d in item["updates"]:
        try:
            await context.application.update_queue.put(Update.de_json(d, context.bot))
        except Exception as e:
            logging.warning("resend failed for %s: %s", uid, e)


async def post_init(app):
    """قايمة الأوامر: البياع يشوف /pay بس، والمحاسب يشوف الكل."""
    await ensure_roles()
    kick_reports()
    await app.bot.set_my_commands([BotCommand("pay", "التحصيلات (دفعة عميل)")])
    admin_cmds = [BotCommand("pay", "التحصيلات (دفعة عميل)"), BotCommand("supplier", "موردين"),
                  BotCommand("custody", "عهدة"), BotCommand("advance", "سلفة"),
                  BotCommand("expense", "مصروفات"), BotCommand("income", "إيرادات"), BotCommand("sale", "مبيعات"),
                  BotCommand("custody_return", "رد عهدة"), BotCommand("advance_return", "رد سلفة"),
                  BotCommand("transfer", "تحويل بين الحسابات"), BotCommand("buy", "شراء أصل"), BotCommand("editentry", "تعديل / حذف قيد"), BotCommand("newentry", "إنشاء قيد"),
                    BotCommand("treasury", "الخزنة"), BotCommand("search", "بحث")]
    for uid in CEO_IDS - ADMINS:
        try:
            await app.bot.set_my_commands([BotCommand("start", "التقارير")], scope=BotCommandScopeChat(uid))
        except Exception as e:
            logging.warning("couldn't set commands for CEO %s: %s", uid, e)
    for uid in ADMINS:
        try:
            await app.bot.set_my_commands(admin_cmds + ([BotCommand("broadcast", "إذاعة رسالة"), BotCommand("actas", "العمل كمستخدم"), BotCommand("me", "رجوع لحسابي"),
                                                     BotCommand("azkar", "إرسال الأذكار دلوقتي")] if uid == OWNER_ID else []),
                                          scope=BotCommandScopeChat(uid))
        except Exception as e:
            logging.warning("couldn't set commands for %s: %s", uid, e)
    if not AZKAR_OFF and AZKAR_TIMES:
        app.create_task(azkar_loop(app))
    await notify_missed(app)


# ---------------- تسوية عهدة المسؤول عن عهدة (م/ محمد رافت) ----------------
# اسم البياع زي ما هو في تاب Lists  ←  اسم مستلم العهدة (بيتكتب في الشيت بدل أسماء الموظفين)
CUSTODY_HOLDERS = {"م/ محمد رافت": "محمد رأفت حلمى عكاشة"}
R_DATE, R_BAYAN, R_AMOUNT, R_NOTE, R_CONFIRM = range(40, 45)
RC_HEADER = ["التاريخ", "البيان", "المبلغ", "الملاحظات", "اسم مستلم العهدة", "المسجّل", "وقت التسجيل",
             "الحالة", "رقم الطلب", "رقم تليجرام", "مرفق"]


INSTALL_HOLDER = ("م/ محمد رافت", "محمد رأفت حلمى عكاشة")      # مدير التركيبات


def _hk(s):
    return norm(re.sub(r"م\s*/\s*", "", s or "")).replace(" ", "")


def holder_of(name):
    for k, v in CUSTODY_HOLDERS.items():
        if _hk(k) == _hk(name):
            return k, v
    return None, None


def rc_markup(rows, back=True):
    rows = [list(r) for r in rows]
    if back:
        rows.append([InlineKeyboardButton("⬅️ رجوع", callback_data="rcback")])
    return InlineKeyboardMarkup(rows)


def rc_summary(d):
    return (
        "راجع التسوية:\n"
        f"التاريخ: {d['date']}\n"
        f"البيان: {d['bayan']}\n"
        f"المبلغ: {d['amount']:,.2f}\n"
        f"الملاحظات: {d['note'] or '-'}\n"
        f"مستلم العهدة: {d['recipient']}"
        + ("\nالمرفق: ✅" if d.get("att") else "")
    )


async def rc_prompt(update, context, state):
    d = context.user_data["rc"]
    if state == R_DATE:
        await say(update, "تاريخ التسوية؟ اضغط اليوم أو اكتبه بالشكل ده: 25/10/2026",
                  rc_markup([[InlineKeyboardButton("اليوم", callback_data="rc:today")]]))
    elif state == R_BAYAN:
        await say(update, "اكتب البيان:", rc_markup([]))
    elif state == R_AMOUNT:
        await say(update, "اكتب المبلغ (أرقام بس):", rc_markup([]))
    elif state == R_NOTE:
        await say(update, "ملاحظات؟ اكتبها أو اضغط تخطي:",
                  rc_markup([[InlineKeyboardButton("تخطي", callback_data="rc:skip")]]))
    elif state == R_ATT:
        await say(update, ATT_PROMPT, rc_markup([[InlineKeyboardButton("تخطي", callback_data="at:skip")]]))
    elif state == R_CONFIRM:
        await say(update, rc_summary(d), rc_markup([[
            InlineKeyboardButton("تأكيد وتسجيل", callback_data="rc:go"),
            InlineKeyboardButton("إلغاء", callback_data="rc:cancel"),
        ]]))


async def rc_go(update, context, state):
    context.user_data.setdefault("rc_hist", []).append(state)
    await rc_prompt(update, context, state)
    return state


async def rc_back(update, context):
    hist = context.user_data.get("rc_hist")
    if not hist or "rc" not in context.user_data:
        await update.callback_query.answer()
        return None
    if len(hist) <= 1:              # أول خطوة: رجوع لقايمة محمد رافت
        context.user_data.clear()
        await send_holder_menu(update, context)
        return ConversationHandler.END
    hist.pop()
    state = hist[-1]
    await rc_prompt(update, context, state)
    return state


async def rc_start(update, context):
    await ensure_roles()
    await update.callback_query.answer()
    uid = update.effective_user.id
    name, role = await run(find_user, uid)
    holder, recipient = holder_of(name)
    if not holder and role == "install_mgr":
        holder, recipient = INSTALL_HOLDER
    if not holder:
        await update.effective_message.reply_text("الخيار ده مش متاح لحسابك.")
        return ConversationHandler.END
    context.user_data.clear()
    context.user_data["rc"] = {"seller": name, "holder": holder, "recipient": recipient}
    context.user_data["rc_hist"] = []
    return await rc_go(update, context, R_DATE)


async def rc_date(update, context):
    d = context.user_data["rc"]
    if update.callback_query:
        d["date"] = datetime.now().strftime("%Y-%m-%d")
    else:
        t = update.message.text.translate(AR_DIGITS).strip()
        try:
            d["date"] = datetime.strptime(t, "%d/%m/%Y").strftime("%Y-%m-%d")
        except ValueError:
            await update.message.reply_text("التاريخ غلط. اكتبه كده: 25/10/2026")
            return R_DATE
    return await rc_go(update, context, R_BAYAN)


async def rc_bayan(update, context):
    t = update.message.text.strip()
    if not t:
        await update.message.reply_text("اكتب البيان:")
        return R_BAYAN
    context.user_data["rc"]["bayan"] = t
    return await rc_go(update, context, R_AMOUNT)


async def rc_amount(update, context):
    t = update.message.text.translate(AR_DIGITS).replace(",", "").strip()
    try:
        amount = float(t)
        assert amount > 0
    except Exception:
        await update.message.reply_text("المبلغ غلط. اكتب أرقام بس، مثلًا 2500:")
        return R_AMOUNT
    context.user_data["rc"]["amount"] = amount
    return await rc_go(update, context, R_NOTE)


async def rc_note(update, context):
    context.user_data["rc"]["note"] = "" if update.callback_query else update.message.text.strip()
    return await rc_go(update, context, R_ATT)


def _safe_cell(t):
    return "'" + t if t[:1] in ("=", "+", "-", "@") else t


def rc_ws(holder):
    """بيرجّع تاب "تسوية عهدة م/ ..." وبيعمله (أو بيحدّث عناوينه) لو لازم."""
    title = f"تسوية عهدة {holder}"
    ws, new = _tab(title, 1000, 14)
    hdr = [h.strip() for h in ws.row_values(1)] if not new else []
    if new or hdr[:len(RC_HEADER)] != RC_HEADER:
        ws.update("A1", [RC_HEADER])
        if not new:
            ws.batch_clear(["I2"])   # مكان إجمالي قديم
        ws.update("L1", [["إجمالي المسجّل في القيود", "إجمالي المعلّق"]])
        ws.update("L2", [['=SUMIFS(C2:C,H2:H,"اتسجّل")', '=SUMIFS(C2:C,H2:H,"معلّق")+SUMIFS(C2:C,H2:H,"مستني اعتماد أحمد")+SUMIFS(C2:C,H2:H,"مستني اعتماد جمعة")+SUMIFS(C2:C,H2:H,"معتمد")']],
                  value_input_option="USER_ENTERED")
    return ws, title


def ensure_rc_tabs():
    for holder in CUSTODY_HOLDERS:
        try:
            rc_ws(holder)
        except Exception as e:
            logging.warning("couldn't create custody settlement tab for %s: %s", holder, e)


def rc_save(d):
    """بيسجل التسوية في التاب بحالة "معلّق" (القيد مش بيتسجل غير لما المحاسب يؤكد)."""
    ws, title = rc_ws(d["holder"])
    rid = "R" + uuid.uuid4().hex[:5]      # بيبدأ بحرف عشان الشيت ما يفتكرهوش رقم
    _append(ws, [d["date"], _safe_cell(d["bayan"]), d["amount"], _safe_cell(d["note"]), d["recipient"],
                 d["seller"], datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "معلّق", rid, d["uid"],
                 att_ref(d.get("att"))])
    return title, rid


def rq_text(d, title):
    return (
        f"{title}\n"
        f"التاريخ: {d['date']}\n"
        f"البيان: {d['bayan']}\n"
        f"المبلغ: {d['amount']:,.2f}\n"
        f"الملاحظات: {d['note'] or '-'}\n"
        f"مستلم العهدة: {d['recipient']}\n\n"
        f"القيد: مدين {SETTLE_L1} / {SETTLE_L2}، دائن العهد_المؤقتة / {d['recipient'].strip()}."
    )


async def rc_confirm(update, context):
    d = context.user_data.get("rc")
    if not d:
        await update.callback_query.answer()
        return ConversationHandler.END
    d["uid"] = update.effective_user.id
    title, rid = await run(rc_save, d)
    await say(update, "اتبعتت لمسؤول الحسابات للتأكيد ✅"
              + ("\nوالصورة هتتعرض عليه للمراجعة." if d.get("att") else "")
              + "\nهيوصلك رد أول ما يتسجل.")
    try:
        m_rq = await context.bot.send_message(
            OWNER_ID, rq_text(d, f"📥 طلب تسوية عهدة من {d['seller']}"),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("تأكيد وتسجيل القيد", callback_data=f"rq:go:{rid}")],
                [InlineKeyboardButton("اعتماد م/ أحمد رافت", callback_data=f"rq:gm:{rid}")],
                [InlineKeyboardButton("رفض", callback_data=f"rq:no:{rid}")],
            ]))
        msg_track(f"q{rid}", m_rq)
        if d.get("att"):
            await send_att(context, OWNER_ID, att_ref(d["att"]), f"مرفق تسوية العهدة ({d['seller']})", key=f"q{rid}")
    except Exception as e:
        logging.warning("couldn't notify owner about settlement %s: %s", rid, e)
    context.user_data.pop("rc", None)
    return ConversationHandler.END


async def rc_cancel(update, context):
    context.user_data.pop("rc", None)
    await say(update, "اتلغت. ابعت /start لو عايز تبدأ من جديد.")
    return ConversationHandler.END


# ---------------- تأكيد تسوية العهدة وتسجيل قيدها ----------------
def rq_find(rid):
    for holder in CUSTODY_HOLDERS:
        ws, _ = rc_ws(holder)
        cell = ws.find(rid, in_column=9)
        if cell:
            vals = ws.row_values(cell.row, value_render_option="UNFORMATTED_VALUE")
            vals = vals + [""] * (11 - len(vals))
            return ws, cell.row, vals
    return None, None, None


def rq_row_to_d(v):
    from datetime import timedelta
    dt = v[0]
    if isinstance(dt, (int, float)):
        date = (datetime(1899, 12, 30) + timedelta(days=dt)).strftime("%Y-%m-%d")
    else:
        date = str(dt).strip()
    return {"date": date, "bayan": str(v[1]), "amount": num(v[2]), "note": str(v[3]),
            "recipient": str(v[4]), "seller": str(v[5]), "status": str(v[7]),
            "tg": str(int(v[9])) if isinstance(v[9], (int, float)) else str(v[9]).strip(),
            "att": str(v[10])}


def rq_post(d, rid, user):
    """مدين انتقالات، دائن العهد المؤقتة على المسؤول عن العهدة (نفس قيد م/ محمد حسن)."""
    desc = f"تسوية عهدة {d['recipient'].strip()} - انتقالات - {d['bayan']}" + (f" ({d['note']})" if d["note"] else "")
    mk = lambda l1, l2, l3, l4, dr, cr: [user, d["date"], d.get("voucher", ""), desc, l1, l2, l3, l4, 0, "", "", "", "", "", "", dr, cr]
    lines = [mk(SETTLE_L1, SETTLE_L2, "", "", d["amount"], ""),
             mk(CUS_L1, CUS_L2, "العهد_المؤقتة", d["recipient"], "", d["amount"])]
    gl_append(lines)
    _GL_CACHE["t"] = 0
    try:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        logs = [[now, d["date"], user, "تسوية عهدة", d["recipient"].strip(), d["amount"], "تسوية من تليجرام", desc]]
        append_emp_log("Custody_Log", logs, "صرف عهدة", ("تسوية عهدة", "رد عهدة"), "العهدة", "التسوية")
        append_log("Expenses_Log", LOG_HEADER, logs)
    except Exception as e:
        logging.warning("settlement logs failed: %s", e)


async def rq_cb(update, context):
    await ensure_roles()
    q = update.callback_query
    uid = q.from_user.id
    try:
        _, act, rid = q.data.split(":")
    except ValueError:
        await q.answer()
        return
    gomaa_act = act in ("ga", "gr")
    if (gomaa_act and uid not in CEO_IDS) or (not gomaa_act and uid != OWNER_ID):
        await q.answer("غير مسموح", show_alert=True)
        return
    await q.answer()
    ws, row, vals = await run(rq_find, rid)
    if not row:
        await q.message.reply_text("الطلب مش موجود في الشيت.")
        return
    d = rq_row_to_d(vals)
    if d["status"] in ("اتسجّل", "مرفوض"):
        await q.edit_message_text(f"الطلب ده اتعالج قبل كده ({d['status']}).")
        return

    async def tell_seller(text):
        if d["tg"].isdigit():
            try:
                await context.bot.send_message(int(d["tg"]), text)
            except Exception:
                pass

    if act == "go":
        user = ADMIN_USERS.get(OWNER_ID, ACCOUNTANTS[0])
        path = await save_ref(context, "تسوية العهد", d["seller"], d.get("att", ""), d.get("bayan", ""), d.get("date", "")) if d.get("att", "").startswith("tg:") else ""
        await run(rq_post, d, rid, user)
        await run(ws.update, f"H{row}", [["اتسجّل"]])
        if path:
            await run(ws.update, f"K{row}", [[path]])
        await q.edit_message_text(rq_text(d, "تسوية عهدة") + "\n\n✅ اتسجّل القيد في GL_Entries.")
        await msg_purge(context.bot, f"q{rid}", update)
        await tell_seller(f"تسوية العهدة بتاعتك ({d['amount']:,.2f}) اتأكدت واتسجّلت ✅")
    elif act == "no":
        await run(ws.update, f"H{row}", [["مرفوض"]])
        await q.edit_message_text(rq_text(d, "تسوية عهدة") + "\n\n❌ اترفضت.")
        await msg_purge(context.bot, f"q{rid}", update)
        await tell_seller("تسوية العهدة بتاعتك اترفضت. اتواصل مع مسؤول الحسابات.")
    elif act == "gm":
        if not CEO_IDS:
            await q.message.reply_text("مفيش حد بدور CEO في تاب Users_IDs. اكتب دور م/ أحمد رافت CEO الأول.")
            return
        await run(ws.update, f"H{row}", [["مستني اعتماد أحمد"]])
        for cid in CEO_IDS:
            try:
                await context.bot.send_message(
                    cid, f"{greeting()} {CEO_NAME} ❀\nصلي على محمد ﷺ ❀\n\n" + rq_text(d, f"📥 طلب اعتماد تسوية عهدة ({d['seller']})"),
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("اعتماد", callback_data=f"rq:ga:{rid}"),
                        InlineKeyboardButton("عدم اعتماد", callback_data=f"rq:gr:{rid}")]]))
                if d.get("att"):
                    await send_att(context, cid, d["att"], f"مرفق التسوية ({d['seller']})")
            except Exception as e:
                logging.warning("couldn't send approval request to CEO %s: %s", cid, e)
        await q.edit_message_text(q.message.text + "\n\n⏳ اتبعتت لـ م/ أحمد رافت للاعتماد.")
    elif act == "ga":
        await run(ws.update, f"H{row}", [["معتمد"]])
        await q.edit_message_text(q.message.text + "\n\n✅ تم الاعتماد.")
        await context.bot.send_message(
            OWNER_ID, "تم الاعتماد من م/ أحمد رافت ✅\n\n" + rq_text(d, f"تسوية عهدة ({d['seller']})"),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("تأكيد وتسجيل القيد", callback_data=f"rq:go:{rid}")]]))
    elif act == "gr":
        await run(ws.update, f"H{row}", [["معلّق"]])
        await q.edit_message_text(q.message.text + "\n\n❌ تم رفض الاعتماد.")
        await context.bot.send_message(OWNER_ID, f"م/ أحمد رافت رفض اعتماد تسوية العهدة ({d['seller']}، {d['amount']:,.2f}) ❌\nالقيد لسه مش متسجل.",
                                       reply_markup=InlineKeyboardMarkup([[
                                           InlineKeyboardButton("رفض نهائي", callback_data=f"rq:no:{rid}"),
                                           InlineKeyboardButton("تأكيد وتسجيل القيد", callback_data=f"rq:go:{rid}")]]))


# ================= المرفقات (صور العمليات) =================
# بتتحفظ على الجهاز اللي البوت شغال عليه، في فولدر "مرفقات" جنب bot.py:
#   مرفقات / نوع العملية / اسم المستخدم / الصورة
# لو عايز مكان تاني حط في .env:  ATTACH_DIR=D:\مرفقات
ATTACH_DIR = os.environ.get("ATTACH_DIR", "").strip() or os.path.join(os.path.dirname(os.path.abspath(__file__)), "مرفقات")
# درايف (اختياري): لو حطيت DRIVE_PARENT_ID البوت بيحاول يرفع نسخة كمان. لو فشل بيكمل عادي والصورة محفوظة على الجهاز.
DRIVE_PARENT_ID = os.environ.get("DRIVE_PARENT_ID", "").strip().split("?")[0].strip("/")
CAT = {"pay": "التحصيلات", "sup": "الموردين", "exp": "المصروفات", "inc": "الإيرادات", "cus": "العهد", "adv": "السلف",
       "cret": "رد العهد", "aret": "رد السلف", "sale": "المبيعات", "xfer": "التحويلات", "buy": "المشتريات"}


def sd_cat(d):
    return "تسوية العهد" if d.get("settle") else CAT.get(d.get("flow"), "أخرى")


def _fs(s):
    return re.sub(r'[\\/:*?"<>|]+', "-", (s or "").strip()) or "مجهول"


def att_ref(a):
    """المرفق بيتخزّن في الشيت مؤقتًا كـ tg:<id>|p لحد ما المحاسب يؤكد، وبعدها بيتبدل بمكان الصورة."""
    return f"tg:{a['id']}|{'d' if a.get('doc') else 'p'}" if a else ""


def parse_ref(ref):
    m = re.match(r"tg:(.+)\|([dp])$", str(ref or "").strip())
    return (m.group(1), m.group(2) == "d") if m else (None, False)


def att_from_msg(msg):
    if msg.photo:
        f = msg.photo[-1]
        return {"id": f.file_id, "doc": False}
    doc = msg.document
    if doc and (doc.mime_type or "").startswith("image/"):
        return {"id": doc.file_id, "doc": True}
    return None


def _drive():
    from googleapiclient.discovery import build
    dc = Credentials.from_service_account_file(SA_FILE, scopes=["https://www.googleapis.com/auth/drive"])
    return build("drive", "v3", credentials=dc, cache_discovery=False)


def _q(s):
    return s.replace("\\", "\\\\").replace("'", "\\'")


def _dfolder(svc, name, parent):
    q = f"name = '{_q(name)}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false and '{parent}' in parents"
    res = svc.files().list(q=q, fields="files(id)", supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
    if res.get("files"):
        return res["files"][0]["id"]
    meta = {"name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent]}
    return svc.files().create(body=meta, fields="id", supportsAllDrives=True).execute()["id"]


def drive_upload(cat, user, data, filename, mime):
    from googleapiclient.http import MediaIoBaseUpload
    svc = _drive()
    fid = _dfolder(svc, _fs(user), _dfolder(svc, _fs(cat), DRIVE_PARENT_ID))
    media = MediaIoBaseUpload(io.BytesIO(data), mimetype=mime, resumable=False)
    f = svc.files().create(body={"name": filename, "parents": [fid]}, media_body=media,
                           fields="id,webViewLink", supportsAllDrives=True).execute()
    return f.get("webViewLink") or f"https://drive.google.com/file/d/{f['id']}/view"


def save_attachment_sync(cat, user, data, ext, label="", date=""):
    folder = os.path.join(ATTACH_DIR, _fs(cat), _fs(user))
    os.makedirs(folder, exist_ok=True)
    stem = re.sub(r"\s+", " ", f"{date} - {label}" if (label and date) else f"{date}{label}").strip(" -")
    stem = _fs(stem)[:90].strip(" .") if stem else ""
    if stem:
        name, n = stem + ext, 2
        while os.path.exists(os.path.join(folder, name)):
            name, n = f"{stem} ({n}){ext}", n + 1
    else:
        name = datetime.now().strftime("%Y%m%d_%H%M%S") + f"_{uuid.uuid4().hex[:4]}{ext}"
    path = os.path.join(folder, name)
    with open(path, "wb") as fh:
        fh.write(data)
    out = path
    if DRIVE_PARENT_ID:
        try:
            out += " | " + drive_upload(cat, user, data, name, "image/jpeg")
        except Exception as e:
            logging.warning("drive upload skipped: %s", e)
    return out


async def save_ref(context, cat, user, ref, label="", date=""):
    """بينزّل الصورة من تليجرام ويحفظها في مرفقات / cat / user. بيرجّع مكانها (أو فاضي لو فشل)."""
    fid, _ = parse_ref(ref)
    if not fid:
        return ""
    try:
        f = await context.bot.get_file(fid)
        data = bytes(await f.download_as_bytearray())
        ext = os.path.splitext(f.file_path or "")[1] or ".jpg"
        return await run(save_attachment_sync, cat, user, data, ext, label, date)
    except Exception as e:
        logging.warning("attachment save failed: %s", e)
        return ""


async def send_att(context, chat_id, ref, caption, key=None):
    fid, is_doc = parse_ref(ref)
    if not fid:
        return
    try:
        if is_doc:
            m = await context.bot.send_document(chat_id, fid, caption=caption)
        else:
            m = await context.bot.send_photo(chat_id, fid, caption=caption)
        if key:
            msg_track(key, m)
    except Exception as e:
        logging.warning("couldn't send attachment: %s", e)


ATT_PROMPT = "تحب تضيف مرفق؟ ابعت صورة (اختياري)، أو اضغط تخطي:"


async def take_att(update, context, store, this_state, next_state, goer):
    """بيستقبل صورة المرفق (أو تخطي) وبيبعت رسالة تأكيد، وبعدين بيكمل للخطوة اللي بعدها."""
    if update.callback_query:
        store["att"] = None
        return await goer(update, context, next_state)
    a = att_from_msg(update.message)
    if not a:
        await update.message.reply_text("ابعت صورة (مش كلام)، أو اضغط تخطي.")
        return this_state
    store["att"] = a
    await update.message.reply_text("تم استلام المرفق ✅")
    return await goer(update, context, next_state)


async def got_attach(update, context):
    return await take_att(update, context, context.user_data["d"], ATTACH, CONFIRM, go)


async def s_att(update, context):
    return await take_att(update, context, context.user_data["sd"], S_ATT, S_CONFIRM, sgo)


async def rc_att(update, context):
    return await take_att(update, context, context.user_data["rc"], R_ATT, R_CONFIRM, rc_go)


async def got_pvoucher(update, context):
    t = update.message.text.translate(AR_DIGITS).strip()
    if not t:
        await update.message.reply_text("رقم الوصل لازم يتكتب. اكتبه:")
        return P_VOUCH
    context.user_data["d"]["voucher"] = t
    return await go(update, context, DATE)


async def sup_voucher(update, context):
    t = update.message.text.translate(AR_DIGITS).strip()
    if not t:
        await update.message.reply_text("رقم الوصل لازم يتكتب. اكتبه:")
        return S_VOUCH
    d = context.user_data["sd"]
    d["voucher"] = t
    return await sgo(update, context, S_CONFIRM if d.get("preset") else S_DATE)


# ================= قوايم حسب الدور =================
async def send_holder_menu(update, context):
    """محمد رافت: تسوية عهدة + التحصيلات. محمد حسن: التحصيلات."""
    uid = update.effective_user.id
    name, role = await run(find_user, uid)
    btns = [InlineKeyboardButton("التحصيلات (دفعة عميل)", callback_data="hm:pay")]
    if role == "install_mgr" or holder_of(name)[0]:
        btns = [InlineKeyboardButton("تسوية عهدة", callback_data="hm:cus")] + btns
    rows = [btns]
    if role == "sales_mgr":
        rows.append([InlineKeyboardButton("العهدة الشخصية", callback_data="rv:mcus"),
                     InlineKeyboardButton("التقارير", callback_data="rv:grep")])
    await say(update, f"{greeting()} {update.effective_user.first_name} ❀\nصلي على محمد ﷺ ❀\n\nتفتح إيه؟",
              InlineKeyboardMarkup(rows))


def gomaa_menu():
    return InlineKeyboardMarkup([[InlineKeyboardButton("العهدة", callback_data="gm:cus")],
                                 [InlineKeyboardButton("التقارير", callback_data="gm:rp")]])


def ceo_menu(uid):
    """أول حاجة تظهر لـ م/ أحمد: تحويل، اعتمادات، تقارير بس."""
    return InlineKeyboardMarkup([[InlineKeyboardButton("السيولة", callback_data="rv:liq")],
                                 [InlineKeyboardButton("المستجدات", callback_data="ceo:news")],
                                 [InlineKeyboardButton("التقارير", callback_data="rp:menu")]])


def ceo_reports_markup(uid):
    rows = [list(r) for r in report_menu(uid).inline_keyboard]
    rows.append([InlineKeyboardButton("⬅️ رجوع", callback_data="rp:home")])
    return InlineKeyboardMarkup(rows)


def gomaa_report(who=None):
    """إجمالي تحصيلات البياعين (دفعات العملاء المسجّلة) + تسوية العهدات. م/ جمعه وم/ محمد حسن بيشوفوا عهدتهم هم بس."""
    _GL_CACHE["t"] = 0
    own = {"gm": norm(CUSTODY_PERSON), "mh": norm(SETTLE_EMP)}.get(who)
    pay, stl, tp, ts = {}, {}, 0.0, 0.0
    for r in read_gl():
        desc, cr = r[3].strip(), num(r[16])
        if cr <= 0:
            continue
        if r[5].strip() == "العملاء" and " تبع " in desc:
            who = r[12].strip() or desc.split(" تبع ")[-1].strip() or "بدون اسم"
            pay[who] = pay.get(who, 0) + cr
            tp += cr
        elif r[5].strip() == "مدينون_وارصده_مدينة" and r[6].strip() == "العهد_المؤقتة" and desc.startswith("تسوية عهدة"):
            if own and norm(r[7]) != own:
                continue
            who = r[7].strip() or "بدون اسم"
            stl[who] = stl.get(who, 0) + cr
            ts += cr

    def lines(d):
        return "\n".join(rep_item(k, v) for k, v in sorted(d.items(), key=lambda kv: -kv[1])) or "▫️ مفيش"
    return "\n".join([rep_head("📊", "التقرير"), "",
                      "💰 <b>التحصيلات</b> <i>(دفعات البياعين المسجّلة)</i>", f"✅ الإجمالي: <b>{money(tp)} ج</b>", "",
                      lines(pay), "", LINE, "",
                      "🧾 <b>تسوية العهدات</b>", f"✅ الإجمالي: <b>{money(ts)} ج</b>", "", lines(stl)])


def gomaa_custody_report():
    """عهدة م/ محمد جمعة الشخصية: المستلم (مدين) والمنصرف (دائن) والرصيد الحالي."""
    _GL_CACHE["t"] = 0
    me = norm(CUSTODY_PERSON)
    got = spent = 0.0
    for r in read_gl():
        if r[5].strip() == CUSTODY_L2 and r[6].strip() in CUSTODY_TYPES and norm(r[7]) == me:
            got += num(r[15])
            spent += num(r[16])
    bal = got - spent
    warn = "\n⚠️ المنصرف أكبر من المستلم" if bal < -0.5 else ""
    return "\n".join([rep_head("👤", "عهدتك الشخصية"), "",
                      f"📥 المستلم من العهدة: <b>{money(got)} ج</b>",
                      f"📤 المنصرف: <b>{money(spent)} ج</b>", "", LINE,
                      f"💼 <b>الرصيد الحالي: {money(bal)} ج</b>{warn}", "",
                      f"<i>آخر تحديث: {datetime.now().strftime('%Y-%m-%d %H:%M')}</i>"])


async def gm_cb(update, context):
    await ensure_roles()
    q = update.callback_query
    if not GOMAA_ID or q.from_user.id != GOMAA_ID:
        await q.answer("غير مسموح", show_alert=True)
        return
    await q.answer()
    act = q.data.split(":")[1]
    back_row = [InlineKeyboardButton("⬅️ رجوع", callback_data="gm:menu")]
    if act == "menu":
        if MINIAPP_URL:
            await q.edit_message_text("تمام 👍 اختار من الأزرار اللي تحت.")
            return
        await q.edit_message_text("حضرتك تختار إيه؟", reply_markup=gomaa_menu())
    elif act == "ap":
        ib = context.application.bot_data.get("ib")
        if ib and ib.get("status") == "await_gomaa":
            text = settle_message(ib["rows"], ib["other"], "📥 طلب اعتماد تسويات انتقالات (من م/ محمد حسن)")
            await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("اعتماد", callback_data=f"ib:ga:{ib['id']}"),
                InlineKeyboardButton("عدم اعتماد", callback_data=f"ib:gr:{ib['id']}")], back_row]))
        else:
            await q.edit_message_text("مفيش اعتمادات مستنية حضرتك دلوقتي ✅", reply_markup=InlineKeyboardMarkup([back_row]))
    elif act == "rp":
        await q.edit_message_text(cut_text(await run(gomaa_report, "gm")), reply_markup=InlineKeyboardMarkup([back_row]), parse_mode="HTML")
    elif act == "cus":
        await q.edit_message_text(cut_text(await run(gomaa_custody_report)), reply_markup=InlineKeyboardMarkup([back_row]), parse_mode="HTML")


async def inc_start(update, context):
    await ensure_roles()
    if update.effective_user.id not in ADMINS:
        if update.callback_query:
            await update.callback_query.answer("للمحاسبين بس", show_alert=True)
        return ConversationHandler.END
    if not await run(income_accounts):
        if update.callback_query:
            await update.callback_query.answer()
        await update.effective_message.reply_text("تاب Income_Accounts في الشيت فاضي. اكتب فيه حسابات الإيرادات (المستوى1 | المستوى2 | المستوى3) من اليومية.")
        return ConversationHandler.END
    return await owner_form_open(update, context, "inc")


# ================= أمين الخزنة: مصروف / إيراد من تليجرام (بعد اعتماد المحاسب بس) =================
# قايمة حسابات الإيرادات اللي بتختار منها لما تعتمد إيراد الخزنة: [("المستوى1", "المستوى2", "المستوى3"), ...]
INCOME_ACCOUNTS = []
CASH_PEND_TAB = "Cash_Pending"
CASH_PEND_HEADER = ["رقم", "وقت التسجيل", "التاريخ", "رقم الوصل", "البيان", "المبلغ", "النوع", "الطرف", "الخزنة",
                    "أمين الخزنة", "الحالة", "الحساب", "مرفق", "رقم تليجرام"]


def cm_kb(rows):
    rows = [list(r) for r in rows] + [[InlineKeyboardButton("⬅️ رجوع", callback_data="cm:back"),
                                       InlineKeyboardButton("إلغاء", callback_data="cm:cancel")]]
    return InlineKeyboardMarkup(rows)


def cm_summary(d):
    inc = d["kind"] == "inc"
    return ("راجع الحركة:\n"
            f"النوع: {'إيراد' if inc else 'مصروف'}\n"
            f"البيان: {d['bayan']}\n"
            f"المبلغ: {d['amount']:,.2f}\n"
            f"التاريخ: {d['date']}\n"
            f"{'اللي دفع' if inc else 'المستلم'}: {d['party']}\n"
            f"رقم الوصل: {d['voucher']}"
            + ("\nالمرفق: ✅" if d.get("att") else ""))


async def cm_prompt(update, context, state):
    d = context.user_data["cm"]
    inc = d["kind"] == "inc"
    if state == C_BAYAN:
        await say(update, "اكتب البيان:", cm_kb([]))
    elif state == C_AMOUNT:
        await say(update, "اكتب المبلغ (أرقام بس):", cm_kb([]))
    elif state == C_DATE:
        await say(update, "التاريخ؟ اضغط اليوم أو اكتبه كده: 25/10/2026",
                  cm_kb([[InlineKeyboardButton("اليوم", callback_data="cm:today")]]))
    elif state == C_PARTY:
        await say(update, "اسم اللي دفع الفلوس:" if inc else "اسم المستلم:", cm_kb([]))
    elif state == C_VOUCH:
        await say(update, "اكتب رقم الوصل (لازم):", cm_kb([]))
    elif state == C_ATT:
        await say(update, ATT_PROMPT, cm_kb([[InlineKeyboardButton("تخطي", callback_data="at:skip")]]))
    elif state == C_CONFIRM:
        await say(update, cm_summary(d), cm_kb([[InlineKeyboardButton("تأكيد وإرسال للمحاسب", callback_data="cm:go")]]))


async def cm_goer(update, context, state):
    context.user_data.setdefault("chist", []).append(state)
    await cm_prompt(update, context, state)
    return state


async def send_cash_menu(update, context):
    uid = update.effective_user.id
    nm = (await run(find_user, uid))[0] or update.effective_user.first_name
    await say(update, f"{greeting()} {nm} ❀\nصلي على محمد ﷺ ❀\n\nتفتح إيه؟", cash_menu_markup())


async def cm_back(update, context):
    hist = context.user_data.get("chist")
    if not hist or "cm" not in context.user_data:
        await update.callback_query.answer()
        return None
    if len(hist) <= 1:              # أول خطوة: رجوع لقايمة أمين الخزنة
        context.user_data.clear()
        await send_cash_menu(update, context)
        return ConversationHandler.END
    hist.pop()
    await cm_prompt(update, context, hist[-1])
    return hist[-1]


async def cm_start(update, context):
    await ensure_roles()
    q = update.callback_query
    uid = update.effective_user.id
    if uid not in CASH_MAIN_IDS and uid not in CASH_SUB_IDS:
        await q.answer("الخيار ده لأمين الخزنة بس", show_alert=True)
        return ConversationHandler.END
    context.user_data.clear()
    kind = q.data.split(":")[1]          # exp أو inc
    context.user_data["cm"] = {"kind": kind, "sub": uid in CASH_SUB_IDS and uid not in CASH_MAIN_IDS}
    await q.answer()
    context.user_data["chist"] = [C_BAYAN]
    await cm_prompt(update, context, C_BAYAN)
    return C_BAYAN


async def cm_bayan(update, context):
    t = update.message.text.strip()
    if not t:
        await update.message.reply_text("اكتب البيان:")
        return C_BAYAN
    context.user_data["cm"]["bayan"] = t
    return await cm_goer(update, context, C_AMOUNT)


async def cm_amount(update, context):
    t = update.message.text.translate(AR_DIGITS).replace(",", "").strip()
    try:
        amount = float(t)
        assert amount > 0
    except Exception:
        await update.message.reply_text("المبلغ غلط. اكتب أرقام بس، مثلًا 2500:")
        return C_AMOUNT
    context.user_data["cm"]["amount"] = amount
    return await cm_goer(update, context, C_DATE)


async def cm_date(update, context):
    d = context.user_data["cm"]
    if update.callback_query:
        await update.callback_query.answer()
        d["date"] = datetime.now().strftime("%Y-%m-%d")
    else:
        t = update.message.text.translate(AR_DIGITS).strip()
        try:
            d["date"] = datetime.strptime(t, "%d/%m/%Y").strftime("%Y-%m-%d")
        except ValueError:
            await update.message.reply_text("التاريخ غلط. اكتبه كده: 25/10/2026")
            return C_DATE
    return await cm_goer(update, context, C_PARTY)


async def cm_party(update, context):
    t = update.message.text.strip()
    if not t:
        await update.message.reply_text("اكتب الاسم:")
        return C_PARTY
    context.user_data["cm"]["party"] = t
    return await cm_goer(update, context, C_VOUCH)


async def cm_voucher(update, context):
    t = update.message.text.translate(AR_DIGITS).strip()
    if not t:
        await update.message.reply_text("رقم الوصل لازم يتكتب. اكتبه:")
        return C_VOUCH
    context.user_data["cm"]["voucher"] = t
    return await cm_goer(update, context, C_ATT)


async def cm_att(update, context):
    return await take_att(update, context, context.user_data["cm"], C_ATT, C_CONFIRM, cm_goer)


def cm_save(d, user, uid):
    """بيسجل الطلب في Cash_Pending بس. مفيش حاجة بتتسجل في الخزنة ولا في القيود غير لما المحاسب يعتمد."""
    inc = d["kind"] == "inc"
    tab = "الخزنة الفرعية" if d["sub"] else "الخزنة الرئيسية"
    stmt = f"{d['bayan']} - " + (f"من {d['party']}" if inc else f"إلى {d['party']}")
    ws, new = _tab(CASH_PEND_TAB, 1000, len(CASH_PEND_HEADER))
    if new or [h.strip() for h in ws.row_values(1)][:len(CASH_PEND_HEADER)] != CASH_PEND_HEADER:
        ws.update("A1", [CASH_PEND_HEADER])
    pid = "C" + uuid.uuid4().hex[:5]
    _append(ws, [pid, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), d["date"], _safe_cell(d["voucher"]), _safe_cell(stmt),
                 d["amount"], "إيراد" if inc else "مصروف", _safe_cell(d["party"]), tab, user, "مستني اعتماد", "",
                 att_ref(d.get("att")), uid])
    return pid, stmt, tab


def cash_menu_markup():
    return InlineKeyboardMarkup([[InlineKeyboardButton("📝 تسجيل حركة (فورم)", callback_data="cm:form")]])


async def cm_submit(context, d, uid, name):
    """بيسجّل طلب الخزنة في Cash_Pending ويبعته للمحاسب. بيرجّع رسالة التأكيد لأمين الخزنة."""
    pid, stmt, tab = await run(cm_save, d, name, uid)
    try:
        m_cx = await context.bot.send_message(
            OWNER_ID, f"📥 طلب جديد على {tab} من {name}\n" + cm_summary(d),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("اعتماد واختيار الحساب", callback_data=f"cx:{pid}:s")],
                [InlineKeyboardButton("اعتماد من غير قيد", callback_data=f"cx:{pid}:n")],
                [InlineKeyboardButton("رفض", callback_data=f"cx:{pid}:r")],
            ]))
        msg_track(f"c{pid}", m_cx)
        if d.get("att"):
            await send_att(context, OWNER_ID, att_ref(d["att"]), f"مرفق الطلب {pid} ({name})", key=f"c{pid}")
    except Exception as e:
        logging.warning("couldn't notify owner about cash move %s: %s", pid, e)
    return ("اتبعتت لمسؤول الحسابات للمراجعة والاعتماد ✅"
            + ("\nوالمرفق هيتعرض عليه للمراجعة." if d.get("att") else "")
            + "\nهيوصلك رد أول ما يتأكد.\n\n" + cm_summary(d))


def cash_again_kb():
    """بعد كل عملية: أمين الخزنة يضيف عملية تانية على طول أو يقفل."""
    return InlineKeyboardMarkup([[InlineKeyboardButton("➕ إضافة عملية أخرى", callback_data="cm:form"),
                                  InlineKeyboardButton("✖️ إغلاق", callback_data="cm:close")]])


async def cash_close(update, context):
    q = update.callback_query
    await q.answer("تمام ✅")
    try:
        await q.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass


async def cm_confirm(update, context):
    d = context.user_data.get("cm")
    if not d:
        await update.callback_query.answer()
        return ConversationHandler.END
    uid = update.effective_user.id
    name = (await run(find_user, uid))[0] or update.effective_user.full_name
    await say(update, await cm_submit(context, d, uid, name), cash_again_kb())
    context.user_data.pop("cm", None)
    return ConversationHandler.END


async def cm_cancel(update, context):
    context.user_data.pop("cm", None)
    await say(update, "اتلغت. ابعت /start لو عايز تبدأ من جديد.")
    return ConversationHandler.END


def cx_find(pid):
    ws, _ = _tab(CASH_PEND_TAB, 1000, len(CASH_PEND_HEADER))
    cell = ws.find(pid, in_column=1)
    if not cell:
        return None, None, None
    v = ws.row_values(cell.row, value_render_option="UNFORMATTED_VALUE")
    v = v + [""] * (14 - len(v))
    rec = {"pid": pid, "date": str(v[2]), "voucher": str(v[3]), "stmt": str(v[4]), "amount": num(v[5]), "kind": str(v[6]),
           "party": str(v[7]), "tab": str(v[8]), "user": str(v[9]), "status": str(v[10]), "att": str(v[12]),
           "tg": str(int(v[13])) if isinstance(v[13], (int, float)) else str(v[13]).strip()}
    if isinstance(v[2], (int, float)):
        from datetime import timedelta
        rec["date"] = (datetime(1899, 12, 30) + timedelta(days=v[2])).strftime("%Y-%m-%d")
    return ws, cell.row, rec


def cash_name(L, sub):
    """اسم الخزنة (زي ما هو في قايمة الخزن في Lists): الفرعية لو فيها كلمة فرع، وإلا الرئيسية."""
    names = L["cash"]
    pick = [n for n in names if ("فرع" in norm(n)) == bool(sub)]
    if not pick:
        raise ValueError("مش لاقي الخزنة " + ("الفرعية" if sub else "الرئيسية") + " في تاب Lists.")
    return next((n for n in pick if sub or "رئيس" in norm(n)), pick[0])


def cash_account(L, sub):
    """حساب الخزنة (L3) من قايمة الخزن في Lists."""
    best = cash_name(L, sub)
    return L["cash_l3"].get(best, best)


def cx_commit(rec, acct, user):
    """بعد اعتماد المحاسب: القيد في GL_Entries (لو اتحدد حساب) وحركة الخزنة في تابها (والشيت التاني لو متظبط)."""
    inc = rec["kind"] == "إيراد"
    amt = rec["amount"]
    if acct:
        L = read_lists()
        cash = (L["l1"], L["l2_cash"], cash_account(L, "فرع" in norm(rec["tab"])), "")
        a4 = tuple(acct)[:3] + ("",)
        row = lambda l1, l2, l3, l4, dr, cr: [user, rec["date"], rec["voucher"], rec["stmt"], l1, l2, l3, l4, 0, "", "", "", "", "", "", dr, cr]
        lines = [row(*cash, amt, ""), row(*a4, "", amt)] if inc else [row(*a4, amt, ""), row(*cash, "", amt)]
        gl_append(lines)
    kind = "إيراد خزنة" if inc else "مصروف خزنة"
    cash_row(rec["tab"], rec["date"], rec["voucher"], rec["stmt"], None if inc else amt, amt if inc else None, kind, rec["user"])
    if not str(rec.get("pid", "")).startswith("S"):
        mirror_cash_row(rec["tab"], [rec["date"], rec["voucher"], rec["stmt"], "" if inc else amt, amt if inc else "", "", kind,
                                     rec["user"], datetime.now().strftime("%Y-%m-%d %H:%M:%S")])


async def cx_finalize(context, q, ws, row, rec, acct, head):
    user = ADMIN_USERS.get(OWNER_ID, ACCOUNTANTS[0])
    inc = rec["kind"] == "إيراد"
    path = ""
    if rec["att"].startswith("tg:"):
        path = await save_ref(context, "إيرادات الخزنة" if inc else "مصروفات الخزنة", rec["user"], rec["att"], rec.get("stmt", ""), rec.get("date", ""))
    await run(cx_commit, rec, acct, user)
    label = " > ".join(x for x in (acct or ())[:3] if x)
    await run(ws.update, f"K{row}:M{row}", [["اتسجّل" if acct else "اتسجّل في الخزنة بس", label, path or rec["att"]]])
    msg = head + ("اتسجّل القيد في GL_Entries ✅\nالحساب: " + label.replace("_", " ") if acct else "اتسجّل في تاب الخزنة بس (من غير قيد) ✅")
    if rec["att"].startswith("tg:"):
        msg += "\nالمرفق اتحفظ ✅" if path else "\n⚠️ المرفق ماتحفظش."
    await q.edit_message_text(msg)
    if rec["tg"].isdigit():
        try:
            await context.bot.send_message(int(rec["tg"]), f"طلبك ({rec['kind']} {rec['amount']:,.2f}) اتأكد واتسجّل ✅")
        except Exception:
            pass


def cx_head(rec):
    return f"{rec['kind']} {rec['amount']:,.2f} | {rec['stmt']} | وصل {rec['voucher']}\n"


def cx_menu_markup(pid):
    btns = [InlineKeyboardButton(n, callback_data=f"cq:{pid}:{k}") for n, k in FULL_OPTS]
    return InlineKeyboardMarkup(pairs(btns) + [[InlineKeyboardButton("اعتماد من غير قيد", callback_data=f"cx:{pid}:n")],
                                               [InlineKeyboardButton("⬅️ رجوع", callback_data=f"cx:{pid}:m")]])


async def cx_back_menu(update, pid):
    ws, row, rec = await run(cx_find, pid)
    if not row or rec["status"] != "مستني اعتماد":
        return False
    await say(update, cx_head(rec) + "تقفل القيد على إيه؟", cx_menu_markup(pid))
    return True


def cx_mark(pid, status):
    ws, row, _ = cx_find(pid)
    if row:
        ws.update(f"K{row}", [[status]])


async def cx_pick(update, context):
    """طلب أمين الخزنة: بتختار القيد يتقفل فين من القايمة كلها. المبلغ والتاريخ ورقم الوصل والبيان والمرفق جاهزين، والخزنة هي الحساب."""
    await ensure_roles()
    q = update.callback_query
    if q.from_user.id != OWNER_ID:
        await q.answer("الزرار ده للمدير بس", show_alert=True)
        return ConversationHandler.END
    _, pid, typ = q.data.split(":")
    ws, row, rec = await run(cx_find, pid)
    if not row or rec["status"] != "مستني اعتماد":
        await q.answer("الطلب ده اتعالج قبل كده", show_alert=True)
        return ConversationHandler.END
    if typ == "inc" and not await run(income_accounts):
        await q.answer()
        await q.message.reply_text("تاب Income_Accounts في الشيت فاضي. اكتب فيه حسابات الإيرادات، أو اعتمد من غير قيد.")
        return ConversationHandler.END
    if typ == "buy" and rec["kind"] == "إيراد":
        await q.answer("شراء أصل بيكون مع المصروفات بس، مش الإيرادات", show_alert=True)
        return ConversationHandler.END
    await q.answer()
    uid = q.from_user.id
    L = await run(read_lists)
    try:
        method = cash_name(L, "فرع" in norm(rec["tab"]))
    except ValueError as e:
        await q.message.reply_text(str(e))
        return ConversationHandler.END
    fid, is_doc = parse_ref(rec["att"])
    inc = rec["kind"] == "إيراد"
    pre = {"preset": True, "cx": pid, "submitter": rec["user"], "amount": rec["amount"], "date": rec["date"],
           "voucher": rec["voucher"], "note": rec["stmt"], "att": {"id": fid, "doc": is_doc} if fid else None}
    cash_acc = {"kind": "cash", "method": method}
    vals = {"att": pre["att"], "amount": rec["amount"], "date": rec["date"], "voucher": rec["voucher"],
            "note": rec["stmt"], "notes": rec["stmt"]}
    vals["acc2" if (typ == "xfer" and inc) else "acc"] = cash_acc
    return await prefilled_form(update, context, typ, {"cx": pid, "submitter": rec["user"], "note": rec["stmt"]}, vals)
    context.user_data.clear()
    if typ == "pay":             # تحصيل عميل داخل الخزنة
        context.user_data["L"] = L
        context.user_data["d"] = {"uid": uid, **pre, "notes": rec["stmt"], "kind": "cash", "method": method,
                                  "other_bank": "", "l3": L["cash_l3"].get(method, method)}
        context.user_data["hist"] = []
        return await go(update, context, SELLER)
    flows = {"exp": ("exp", S_EL1), "inc": ("inc", S_EL1), "sup": ("sup", S_SUP), "cus": ("cus", S_CTYPE),
             "adv": ("adv", S_CTYPE), "xfer": ("xfer", S_KIND)}
    flow, first = flows[typ]
    sd = {"uid": uid, "flow": flow, "kind": "cash", "method": method, "other_bank": "", "phase": "from", **pre}
    if typ == "xfer":
        if inc:      # الخزنة هي اللي استلمت، وإنت بتحدد المصدر
            sd.update(dst_preset=("cash", method, ""), phase="from", kind="", method="", other_bank="")
        else:        # الخزنة هي المصدر، وإنت بتحدد الحساب اللي راح له
            sd.update(src=(method, ""), src_cash=True, phase="to", kind="", method="", other_bank="")
    context.user_data["sL"] = L
    context.user_data["sd"] = sd
    context.user_data["shist"] = []
    return await sgo(update, context, first)


async def cx_close(context, d, lines):
    """بعد تسجيل القيد من طلب الخزنة: نقفل الطلب، ننسخ حركة الخزنة للشيت التاني (لو متظبط)، ونبلّغ أمين الخزنة."""
    try:
        ws, row, rec = await run(cx_find, d["cx"])
        if not row:
            return
        inc = rec["kind"] == "إيراد"
        if d["flow"] in ("exp", "inc"):
            label = " > ".join(x for x in (d.get("l1x"), d.get("l2x"), d.get("l3x"), d.get("l4x")) if x)
        else:
            label = FLOW_LABELS.get(d["flow"], d["flow"])
        await run(ws.update, f"K{row}:M{row}", [["اتسجّل", label, d.get("att_path") or rec["att"]]])
        if not str(rec.get("pid", "")).startswith("S"):
            await run(mirror_cash_row, rec["tab"], [rec["date"], rec["voucher"], lines[0][3], "" if inc else rec["amount"],
                                                    rec["amount"] if inc else "", "", "إيراد خزنة" if inc else "مصروف خزنة",
                                                    rec["user"], datetime.now().strftime("%Y-%m-%d %H:%M:%S")])
        if rec["tg"].isdigit():
            await context.bot.send_message(int(rec["tg"]), f"طلبك ({rec['kind']} {rec['amount']:,.2f}) اتأكد واتسجّل ✅")
    except Exception as e:
        logging.warning("cash request close failed: %s", e)


async def cx_cb(update, context):
    """المحاسب (إنت) بتراجع طلب أمين الخزنة: اعتماد واختيار الحساب، أو اعتماد من غير قيد، أو رفض."""
    q = update.callback_query
    if q.from_user.id != OWNER_ID:
        await q.answer("الزرار ده للمدير بس", show_alert=True)
        return
    await q.answer()
    p = q.data.split(":")
    pid, act = p[1], p[2]
    ws, row, rec = await run(cx_find, pid)
    if not row:
        await q.message.reply_text("الطلب مش موجود.")
        return
    if rec["status"] != "مستني اعتماد":
        await q.edit_message_text(f"الطلب ده اتعالج قبل كده ({rec['status']}).")
        return
    head = cx_head(rec)
    back = [InlineKeyboardButton("اعتماد من غير قيد", callback_data=f"cx:{pid}:n")]
    if act == "m":
        await q.edit_message_text(head + "تعتمد؟", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("اعتماد واختيار الحساب", callback_data=f"cx:{pid}:s")],
            [InlineKeyboardButton("اعتماد من غير قيد", callback_data=f"cx:{pid}:n")],
            [InlineKeyboardButton("رفض", callback_data=f"cx:{pid}:r")]]))
    elif act == "r":
        await run(ws.update, f"K{row}", [["مرفوض"]])
        await q.edit_message_text(head + "❌ اترفض.")
        await msg_purge(context.bot, f"c{pid}", update)
        if rec["tg"].isdigit():
            try:
                await context.bot.send_message(int(rec["tg"]), f"طلبك ({rec['kind']} {rec['amount']:,.2f}) اترفض. اتواصل مع مسؤول الحسابات.")
            except Exception:
                pass
    elif act == "n":
        await cx_finalize(context, q, ws, row, rec, None, head)
        await msg_purge(context.bot, f"c{pid}", update)
    elif act == "s":
        await q.edit_message_text(head + "تقفل القيد على إيه؟", reply_markup=cx_menu_markup(pid))
    elif act == "a":
        i = int(p[3])
        btns = [InlineKeyboardButton(x.replace("_", " "), callback_data=f"cx:{pid}:b:{i}:{j}") for j, x in enumerate(EXP_TREE[EXP_L1[i]])]
        await q.edit_message_text(head + "البند:", reply_markup=InlineKeyboardMarkup(
            pairs(btns) + [back, [InlineKeyboardButton("⬅️ رجوع", callback_data=f"cx:{pid}:s")]]))
    elif act in ("b", "c", "i"):
        if act == "i":
            acct = tuple((await run(income_accounts))[int(p[3])])
        else:
            l1 = EXP_L1[int(p[3])]
            l2 = EXP_TREE[l1][int(p[4])]
            if act == "b" and l2 in EXP_L3:
                i, j = p[3], p[4]
                btns = [InlineKeyboardButton(x.replace("_", " "), callback_data=f"cx:{pid}:c:{i}:{j}:{k}") for k, x in enumerate(EXP_L3[l2])]
                await q.edit_message_text(head + "التفصيل:", reply_markup=InlineKeyboardMarkup(
                    pairs(btns) + [back, [InlineKeyboardButton("⬅️ رجوع", callback_data=f"cx:{pid}:a:{i}")]]))
                return
            acct = (l1, l2, EXP_L3[l2][int(p[5])] if act == "c" else "")
        await cx_finalize(context, q, ws, row, rec, acct, head)
        await msg_purge(context.bot, f"c{pid}", update)


# ================= مسح بيانات التجربة =================
CLEAR_TABS = [("GL_Entries", 2, "Z"), ("Pending", 2, "P"), ("Suppliers_Log", 2, "J"), ("Expenses_Log", 2, "J"),
              ("Income_Log", 2, "J"), ("Custody_Log", 2, "I"), ("Advances_Log", 2, "I"), ("Cash_Pending", 2, "N"),
              ("الخزنة الرئيسية", 3, "I"), ("الخزنة الفرعية", 3, "I"), ("Transfers_Pending", 2, "N")]


def clear_data():
    """بيمسح الصفوف (بيسيب العناوين). مش بيلمس Lists ولا Users_IDs ولا Inbox_State."""
    done = []
    tabs = CLEAR_TABS + [(f"تسوية عهدة {h}", 2, "K") for h in CUSTODY_HOLDERS]
    for name, first, col in tabs:
        try:
            ws = sheet.worksheet(name)
        except gspread.WorksheetNotFound:
            continue
        ws.batch_clear([f"A{first}:{col}{ws.row_count}"])
        done.append(name)
    _GL_CACHE["t"] = 0
    return done


async def clear_cmd(update, context):
    if update.effective_user.id != OWNER_ID:
        return
    await update.message.reply_text(
        "⚠️ ده هيمسح كل العمليات المسجلة (القيود، الطلبات، سجلات الموردين والمصروفات والعهد والسلف، حركات الخزنة، تسويات العهدة).\n"
        "العناوين وتاب Lists وتاب Users_IDs مش هيتلمسوا.\n"
        "المسح مفيهوش رجوع. اتأكد إنك عملت نسخة من الشيت الأول. تمسح؟",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("أيوه امسح", callback_data="cd:go"),
                                            InlineKeyboardButton("إلغاء", callback_data="cd:no")]]))


async def cd_cb(update, context):
    q = update.callback_query
    if q.from_user.id != OWNER_ID:
        await q.answer("غير مسموح", show_alert=True)
        return
    await q.answer()
    if q.data.endswith(":no"):
        await q.edit_message_text("اتلغى. مفيش حاجة اتمسحت.")
        return
    done = await run(clear_data)
    kick_reports()
    await q.edit_message_text("اتمسحت البيانات من: " + "، ".join(done) + "\nالترقيم هيبدأ من 1.")


# ================= شيت التقارير (تاب Reports) =================
REPORTS_TAB = "Reports"
_REP = {"t": 0.0}


def report_blocks(rows):
    """كل تقرير: (العنوان، [(البند, المبلغ)...]، الإجمالي). نفس أرقام تقارير تليجرام."""
    def lst(title, d, only_positive=True):
        items = sorted(((k or "بدون اسم").replace("_", " "), round(v)) for k, v in d.items() if (v > 0 or not only_positive))
        items.sort(key=lambda kv: -kv[1])
        return (title, items, sum(v for _, v in items))

    cash = balances(rows, "النقدية_بالخزينة")
    bank_all = balances(rows, "النقدية_بالبنوك")
    wallets, banks = {}, {}
    for k, v in bank_all.items():
        (wallets if is_wallet(k) else banks)[k] = v
    backup = {k: v for k, v in wallets.items() if "احتياط" in norm(k)}
    main_w = {k: v for k, v in wallets.items() if k not in backup}
    liq = []
    for t, d in (("إجمالي الخزن", cash), ("إجمالي البنوك", banks), ("إجمالي فودافون كاش", main_w), ("إجمالي المحفظة الاحتياطية", backup)):
        liq.append((t, round(sum(d.values()))))
        liq += [("   " + k.replace("_", " "), round(v)) for k, v in sorted(d.items())]
    blocks = [("الموقف النقدي", liq, round(sum(cash.values()) + sum(bank_all.values())))]
    blocks.append(lst("العملاء (المستحق لنا)", balances(rows, "العملاء")))
    blocks.append(lst("الموردين (المستحق علينا)", balances(rows, "الموردين", credit_normal=True)))
    exp = {}
    for r in month_rows(rows):
        if r[4].strip() in EXP_L1:
            exp[r[5].strip()] = exp.get(r[5].strip(), 0) + num(r[15]) - num(r[16])
    blocks.append(lst("مصروفات الشهر", exp))
    tot, cnt = 0.0, 0
    for r in month_rows(rows):
        if r[5].strip() == "الإيرادات_غير_المحققة" and r[6].strip() == "قيمة المبيعات":
            tot += num(r[16]) - num(r[15])
            cnt += 1
    blocks.append(("مبيعات الشهر", [("عدد العمليات", cnt)], round(tot)))
    blocks.append(lst("العهد (اللي عليهم عهد)", cust_balances(rows, ("العهد_المؤقتة", "العهد_المستديمة"))))
    blocks.append(lst("السلف (اللي عليهم سلف)", cust_balances(rows, ("سلف_العاملين",))))
    return blocks


def refresh_reports_sheet():
    _GL_CACHE["t"] = 0
    blocks = report_blocks(read_gl())
    width = len(blocks) * 3 - 1
    height = 3 + max(len(b[1]) for b in blocks)
    grid = [[""] * width for _ in range(height)]
    grid[0][0] = "آخر تحديث: " + datetime.now().strftime("%Y-%m-%d %H:%M")
    for bi, (title, items, total) in enumerate(blocks):
        c = bi * 3
        grid[1][c] = title
        grid[2][c], grid[2][c + 1] = "الإجمالي", total
        for i, (k, v) in enumerate(items):
            grid[3 + i][c], grid[3 + i][c + 1] = _safe_cell(str(k)), v
    ws, _ = _tab(REPORTS_TAB, height + 20, width + 2)
    ws.clear()
    ws.update("A1", grid, value_input_option="RAW")
    try:
        ws.format("A2:" + gspread.utils.rowcol_to_a1(3, width), {"textFormat": {"bold": True}})
        sheet.batch_update({"requests": [{"updateSheetProperties": {
            "properties": {"sheetId": ws.id, "rightToLeft": True}, "fields": "rightToLeft"}}]})
    except Exception as e:
        logging.warning("reports formatting failed: %s", e)


def _safe_refresh():
    try:
        refresh_reports_sheet()
    except Exception as e:
        logging.warning("refresh_reports_sheet failed: %s", e)


def kick_reports():
    """بيحدّث تاب Reports في الخلفية (مرة كل 20 ثانية كحد أقصى) بعد أي قيد جديد."""
    if time.time() - _REP["t"] < 20:
        return
    _REP["t"] = time.time()
    threading.Thread(target=_safe_refresh, daemon=True).start()


async def reports_sheet_cmd(update, context):
    if update.effective_user.id != OWNER_ID:
        return
    try:
        await run(refresh_reports_sheet)
        await update.message.reply_text("تاب Reports اتحدّث ✅")
    except Exception as e:
        await update.message.reply_text(f"تاب Reports ماتحدّثش ({type(e).__name__}): {e}")


# ================= أدوات تشخيص =================
def gl_test_run():
    ws = sheet.worksheet(GL_TAB)
    header = [h for h in ws.row_values(1) if h.strip()]
    before = len(ws.col_values(1))
    ws.append_rows([["TEST", datetime.now().strftime("%Y-%m-%d"), "", "اختبار تسجيل - هيتمسح", "", "", "", "", 0, "", "", "", "", "", "", 1, ""]],
                   value_input_option="USER_ENTERED", table_range="A1")
    after = len(ws.col_values(1))
    ok = after == before + 1
    if after > before:
        ws.delete_rows(after)
    return (f"تاب {GL_TAB}: لقيته ✅\nعدد الأعمدة في العناوين: {len(header)}\n"
            f"الصفوف قبل: {before} | بعد الكتابة: {after}\n"
            + ("النتيجة: الكتابة شغالة تمام ✅ (والصف التجريبي اتمسح)" if ok else "النتيجة: ❌ الكتابة مش بتزيد صفوف. ابعتلي الرسالة دي."))


async def gl_test(update, context):
    if update.effective_user.id != OWNER_ID:
        return
    try:
        await update.message.reply_text(await run(gl_test_run))
    except Exception as e:
        await update.message.reply_text(f"❌ فشل اختبار {GL_TAB} ({type(e).__name__}): {e}")


async def reset_inbox(update, context):
    if update.effective_user.id != OWNER_ID:
        return
    context.application.bot_data.pop("ib", None)
    await update.message.reply_text("اتصفّر طلب الاعتماد المعلّق. افتح تسوية عهدة م/ محمد حسن من جديد.")


async def on_error(update, context):
    """أي خطأ في البوت بيوصلك على تليجرام بدل ما يضيع في الشاشة السودا."""
    err = context.error
    logging.error("bot error", exc_info=err)
    try:
        await context.bot.send_message(OWNER_ID, f"⚠️ خطأ في البوت:\n{type(err).__name__}: {err}"[:3500])
    except Exception:
        pass
    try:
        if isinstance(update, Update) and update.effective_user and update.effective_user.id != OWNER_ID and update.effective_chat:
            await context.bot.send_message(update.effective_chat.id, "حصل خطأ وماتسجلش حاجة. ابعت /start وحاول تاني، ولو اتكرر كلّم المحاسب.")
    except Exception:
        pass


def ensure_pending_cols():
    """عناوين أعمدة المرفق ورقم الوصل في تاب Pending."""
    try:
        ws = sheet.worksheet("Pending")
        ws.update("O1:P1", [["مرفق", "رقم الوصل"]])
    except Exception as e:
        logging.warning("pending header failed: %s", e)


# ================= الفورم (بدل الخطوات): البياع وأمين الخزنة والمحاسب =================
F_VIEW, F_TEXT = 90, 91
FORM_TITLES = {"pay": "فورم دفعة عميل", "opay": "فورم دفعة عميل", "cash": "فورم حركة الخزنة", "sup": "فورم موردين",
               "exp": "فورم مصروف", "inc": "فورم إيراد", "cus": "فورم عهدة", "adv": "فورم سلفة", "sale": "فورم مبيعات",
               "xfer": "فورم تحويل بين الحسابات", "buy": "فورم شراء أصل", "gen": "فورم إنشاء قيد",
               "tsearch": "فورم بحث برقم الوصل", "tcount": "فورم جرد الخزنة (عدّ فعلي)"}
CTYPE_LABELS = [CUSTODY_LABELS[c] for c in CUSTODY_TYPES] + ["رد عهدة", "تسوية عهدة (خطوات)"]
ATYPE_LABELS = ["سلفة", "رد سلفة"]


def _today_str():
    return datetime.now().strftime("%Y-%m-%d")


def pay_acc_options(L, no_cash=False):
    """كل اختيارات الدفع في قايمة واحدة: خزن، بنوك، محافظ. [(النوع, الاسم, الاسم اللي بيظهر)]"""
    opts = [] if no_cash else [("cash", n, "💵 " + n) for n in L["cash"]]
    opts += [("bank", n, "🏦 " + n) for n in L["bank"]]
    opts += [("wallet", n, "📱 " + wallet_label(n)) for n in L["wallet"]]
    return opts


def form_fields(form):
    """الحقول الظاهرة دلوقتي: (المفتاح, الاسم, النوع, لازم؟)."""
    t, v = form["type"], form["vals"]
    amt = ("amount", "المبلغ", "num", True)
    date = ("date", "التاريخ", "date", True)
    att = ("att", "المرفق", "photo", False)

    def acc(label):
        return ("acc", label, "choice", True)

    def vouch(*keys):
        return [("voucher", "رقم الوصل", "text", True)] if any((v.get(k) or {}).get("kind") == "cash" for k in keys) else []
    if t == "gen":
        cash_in = any(len(v.get(k) or []) > 1 and nk(v[k][1]) == nk("النقدية_بالخزينة") for k in ("dr", "cr"))
        return [("dr", "المدين", "tree", True), ("cr", "الدائن", "tree", True), amt, date, ("bayan", "البيان", "text", True),
                ("voucher", "رقم القيد / الوصل" + ("" if cash_in else " (اختياري)"), "text", cash_in),
                ("cc1", "مركز تكلفة1", "text", False), ("cc2", "مركز تكلفة2", "text", False),
                ("client", "اسم العميل", "text", False), ("project", "المشروع", "text", False),
                ("age", "عمر الدين", "text", False), att]
    if t == "tsearch":
        return [("voucher", "رقم الوصل", "text", True)]
    if t == "tcount":
        return ([("tsub", "الخزنة", "choice", True)] if form.get("admin") else []) + \
            [(f"k_{d}", f"فئة {d}", "num", False) for d in DENOMS] + [("k_coin", "فكة (مبلغ)", "num", False), ("tnote", "ملاحظات", "text", False)]
    if t in ("pay", "opay"):
        head = [("seller", "البياع", "choice", True)] if t == "opay" else []
        return head + [("client", "العميل", "text", True), ("ptype", "نوع الدفعة", "choice", True), amt, acc("الدفع تم من")] \
            + vouch("acc") + [("project", "رقم المشروع", "text", False), date, ("notes", "ملاحظات", "text", False), att]
    if t == "cash":
        party = {"إيراد": "اللي دفع", "صرف": "المستلم"}.get(v.get("kind"), "اللي دفع / المستلم")
        return ([("tsub", "الخزنة", "choice", True)] if form.get("admin") else []) + \
            [("kind", "النوع", "choice", True), ("bayan", "البيان", "text", True), amt, date,
             ("party", party, "text", True), ("voucher", "رقم الوصل", "text", True), att]
    if t == "sup":
        f = [("supplier", "المورد", "choice", True), ("stype", "النوع", "choice", True), amt]
        if v.get("stype") == SUPPLIER_TYPES[0]:
            f += [acc("اتدفع من")] + vouch("acc")
        return f + [date, att]
    if t in ("exp", "inc"):
        f = [("l1", "نوع المصروف" if t == "exp" else "نوع الإيراد", "choice", True), ("l2", "البند", "choice", True)]
        if v.get("l2") and form_kids(form, [v.get("l1"), v["l2"]]):
            f.append(("l3", "التفصيل", "choice", True))
        if v.get("l3") and form_kids(form, [v.get("l1"), v.get("l2"), v["l3"]]):
            f.append(("l4", "تفصيل أدق", "choice", True))
        return f + [amt, acc("اتدفع من" if t == "exp" else "اتحصّل في")] + vouch("acc") + [date, ("note", "ملاحظة", "text", False), att]
    if t in ("cus", "adv"):
        first = ("ctype", "نوع العهدة", "choice", True) if t == "cus" else ("atype", "النوع", "choice", True)
        return [first, ("emp", "الموظف", "emp", True), amt, acc("الحساب")] + vouch("acc") + [date, att]
    if t == "buy":
        f = [("aclass", "نوع الأصل", "choice", True)]
        if ASSET_ITEMS.get(v.get("aclass")):
            f.append(("aitem", "الأصل", "choice", False))
        f += [amt, acc("اتدفع من")]
        if (v.get("acc") or {}).get("kind") == "credit":
            f.append(("supplier", "المورد", "choice", True))
        return f + vouch("acc") + [date, ("note", "ملاحظة", "text", False), att]
    if t == "sale":
        return [("seller", "البياع", "choice", True), ("client", "العميل", "text", True), ("project", "رقم المشروع", "text", True),
                amt, date, att]
    return [acc("التحويل من"), ("acc2", "التحويل إلى", "choice", True), amt] + vouch("acc", "acc2") + [date, att]


def form_kids(form, path):
    """أبناء العقدة في فورم المصروفات/الإيرادات (حتى 4 مستويات)."""
    if not path or any(p is None for p in path):
        return []
    if form["type"] == "inc":
        if len(path) == 1:
            return list(form["tree"][1].get(path[0], []))
        if len(path) == 2:
            return list(form["tree"][2].get(path[1], []))
        return []
    return tree_kids(path)


def form_options(form, key):
    """الاسم اللي بيظهر لكل اختيار في حقل الاختيار."""
    L, v = form["L"], form["vals"]
    if key == "ptype":
        return list(L["types"])
    if key in ("acc", "acc2"):
        return [o[2] for o in form["opts"]]
    if key == "seller":
        return list(L["sellers"])
    if key == "aclass":
        return list(ASSET_CLASSES)
    if key == "aitem":
        return list(ASSET_ITEMS.get(v.get("aclass"), []))
    if key == "supplier":
        return list(SUPPLIERS)
    if key == "stype":
        return list(SUPPLIER_TYPES)
    if key == "l1":
        return list(form["tree"][0])
    if key == "l2":
        return form_kids(form, [v.get("l1")])
    if key == "l3":
        return form_kids(form, [v.get("l1"), v.get("l2")])
    if key == "l4":
        return form_kids(form, [v.get("l1"), v.get("l2"), v.get("l3")])
    if key == "ctype":
        return CTYPE_LABELS
    if key == "atype":
        return ATYPE_LABELS
    if key == "emp":
        return [e.strip() for e in form["emp_matches"]]
    if key == "tsub":
        return ["الخزنة الرئيسية", "الخزنة الفرعية"]
    return ["إيراد", "صرف"]


def form_val(form, key):
    v = form["vals"].get(key)
    if not v:
        return ""
    if key in ("dr", "cr"):
        return " > ".join(str(x).replace("_", " ") for x in v)
    if key in ("acc", "acc2"):
        lbl = next((o[2] for o in form["opts"] if o[0] == v["kind"] and o[1] == v["method"]), v["method"])
        other = form["vals"].get("other_bank" if key == "acc" else "other_bank2")
        return lbl + (f" ({other})" if other else "")
    if key == "amount":
        return f"{v:,.2f}"
    if key == "att":
        return "✅ تم"
    if key in ("l1", "l2", "l3", "l4", "aclass", "aitem"):
        return str(v).replace("_", " ")
    return str(v).strip()


def form_text(form, review=False):
    lines = ["📝 " + FORM_TITLES[form["type"]] + (" (مراجعة نهائية)" if review else ""), "━━━━━━━━━━━━━━━"]
    if form["type"] == "pay":
        lines.append(f"البياع: {form['seller']}")
    for key, label, kind, req in form_fields(form):
        val = form_val(form, key)
        lines.append(f"{label}: {val if val else ('⬜' if req or kind == 'photo' else '⬜ (اختياري)')}")
    return "\n".join(lines)


def form_missing(form):
    v = form["vals"]
    miss = [label for key, label, kind, req in form_fields(form) if req and not v.get(key)]
    for k, ok, name in (("acc", "other_bank", "اسم البنك"), ("acc2", "other_bank2", "اسم البنك (إلى)")):
        a = v.get(k)
        if a and a["method"].startswith("أخرى") and not v.get(ok) and any(f[0] == k for f in form_fields(form)):
            miss.append(name)
    return miss


SMALL_CHOICES = {"ptype", "kind", "atype", "ctype", "stype", "tsub"}
DATE_NAMES = ["اليوم", "أمس", "قبل يومين", "قبل 3 أيام"]


def _days_ago(n):
    from datetime import timedelta
    return (datetime.now() - timedelta(days=n)).strftime("%Y-%m-%d")


def form_view(form):
    """(النص, الأزرار) حسب الشاشة الحالية: edit / review / choice / ask."""
    mode = form["mode"]
    back = [InlineKeyboardButton("⬅️ رجوع للفورم", callback_data="f:b")]
    if mode == "tree":
        return tree_view(form)
    if mode == "paste":
        return ("📋 انسخ القيد (أو القيود) من اليومية والصقه هنا في رسالة واحدة.\n"
                "كل سطر = سطر قيد بنفس ترتيب أعمدة اليومية (المستخدم، التاريخ، رقم، البيان، المستوى1..4، العملة، السعر، عمر الدين، مركز تكلفة1، مركز تكلفة2، العميل، المشروع، مدين، دائن).\n"
                "هاخدها زي ما هي وأعرضهالك تراجعها قبل التسجيل.", InlineKeyboardMarkup([back]))
    if mode == "result":
        return form["result"], InlineKeyboardMarkup(form.get("result_kb") or [back])
    if mode == "choice":
        key = form["cur"]
        label = "الموظف" if key == "emp" else next(l for k, l, _, _ in form_fields(form) if k == key)
        opts = form_options(form, key)
        text = f"اختار {label}:"
        if key in ("acc", "acc2"):
            nums = [f"{o[2]}\n({WALLET_NUMBERS[wallet_label(o[1])]})" for o in form["opts"]
                    if o[0] == "wallet" and wallet_label(o[1]) in WALLET_NUMBERS]
            if nums:
                text += "\n\n" + "\n\n".join(nums)
        btns = [InlineKeyboardButton(o.replace("_", " "), callback_data=f"f:v:{i}") for i, o in enumerate(opts)]
        return text, InlineKeyboardMarkup(pairs(btns) + [back])
    if mode == "ask":
        key = form["await"]
        label = {"other_bank": "اسم البنك", "other_bank2": "اسم البنك (إلى)", "date": "التاريخ (مثلًا 25/10/2026)"}.get(key, key)
        return f"✏️ اكتب {label}:", InlineKeyboardMarkup([back])
    if mode == "review":
        return (form_text(form, True) + "\n\nراجع البيانات وبعدها أكّد.",
                InlineKeyboardMarkup([[InlineKeyboardButton("✅ تأكيد وإرسال", callback_data="f:s"),
                                       InlineKeyboardButton("✏️ تعديل", callback_data="f:e")]]))
    v = form["vals"]
    fields = form_fields(form)

    def radio(key):
        return key != "emp" and 0 < len(form_options(form, key)) <= 12

    rows = pairs([InlineKeyboardButton(f"▾ {label}" + (" ✅" if v.get(key) else ""), callback_data=f"f:o:{key}")
                  for key, label, kind, req in fields if (kind == "choice" and not radio(key)) or kind == "tree"])
    if form["type"] == "gen":
        rows.insert(0, [InlineKeyboardButton("📋 لصق قيد منسوخ من اليومية", callback_data="f:paste")])
    for key, label, kind, req in fields:
        if kind == "choice" and radio(key):
            rows.append([InlineKeyboardButton(f"{label}:", callback_data="f:n")])
            btns = [InlineKeyboardButton(("● " if form_selected(form, key, i) else "○ ") + o.replace("_", " "), callback_data=f"f:p:{key}:{i}")
                    for i, o in enumerate(form_options(form, key))]
            per = 2 if key in ("acc", "acc2", "seller") or max(len(o) for o in form_options(form, key)) > 14 else 3
            rows += [btns[i:i + per] for i in range(0, len(btns), per)]
        elif kind == "date":
            today = v.get("date") == _days_ago(0)
            rows.append([InlineKeyboardButton("التاريخ:", callback_data="f:n")])
            rows.append([InlineKeyboardButton(("● " if today else "○ ") + "اليوم", callback_data="f:d:0"),
                         InlineKeyboardButton(("○ " if today else "● ") + "تاريخ تاني (اكتبه)", callback_data="f:d:x")])
    rows.append([InlineKeyboardButton("✅ مراجعة وإرسال", callback_data="f:r"), InlineKeyboardButton("إلغاء", callback_data="f:c")])
    if form["type"] == "tsearch":
        return form_text(form) + "\n\n✍️ اكتب رقم الوصل وهيظهرلك العملية فورًا.", InlineKeyboardMarkup([[InlineKeyboardButton("إغلاق", callback_data="f:c")]])
    if form["type"] == "tcount":
        hint = ("\n\n✍️ اكتب عدد الورق من كل فئة، سطر لكل فئة، كده:\n200: 5\n100: 12\nوالفكة: فكة: 35\n"
                "أي فئة مش هتكتبها تتحسب صفر. وبعدين اضغط مراجعة وإرسال.")
        return form_text(form) + hint, InlineKeyboardMarkup(rows)
    hint = ("\n\n✍️ اكتب الحقول الفاضية في رسالة واحدة، كل حقل في سطر (مثال: العميل: شركة النور) أو بالترتيب. "
            "المرفق: ابعت صورة. الباقي اختار من الدواير تحت.")
    return form_text(form) + hint, InlineKeyboardMarkup(rows)


def form_selected(form, key, i):
    v = form["vals"].get(key)
    if not v:
        return False
    if key in ("acc", "acc2"):
        o = form["opts"][i]
        return v["kind"] == o[0] and v["method"] == o[1]
    return v == form_options(form, key)[i]


async def form_refresh(context, form):
    text, mk = form_view(form)
    try:
        await context.bot.edit_message_text(chat_id=form["chat"], message_id=form["mid"], text=text, reply_markup=mk)
    except Exception as e:      # "message is not modified" وغيره
        logging.info("form refresh skipped: %s", e)


async def form_open(update, context, form):
    if MINIAPP_URL and form["type"] in FORM_TITLES and form["type"] not in NO_MINI and not form.get("native") and (form["uid"] not in ADMINS or ADMIN_MODE.get(form["uid"]) == "f"):
        return await mini_open(update, context, form)
    form.update(mode="edit", **{"await": None})
    context.user_data["form"] = form
    text, mk = form_view(form)
    m = await update.effective_message.reply_text(text, reply_markup=mk)
    form["chat"], form["mid"] = m.chat_id, m.message_id
    return F_VIEW


async def pay_form_open(update, context, seller, att=None):
    uid = update.effective_user.id
    L = await run(read_lists)
    context.user_data.clear()
    form = {"type": "pay", "uid": uid, "seller": seller, "L": L, "opts": pay_acc_options(L, True),
            "vals": {"date": _today_str(), "att": att}}
    return await form_open(update, context, form)


async def owner_form_open(update, context, typ, preset=None):
    """المحاسب: نفس قايمته، بس لما يضغط على حاجة بيفتحله فورم كاملة يملاها."""
    await ensure_roles()
    uid = update.effective_user.id
    if uid not in ADMINS:
        if update.callback_query:
            await update.callback_query.answer("للمحاسبين بس", show_alert=True)
        else:
            await update.message.reply_text("الأمر ده للمحاسبين بس.")
        return ConversationHandler.END
    if typ == "inc" and not await run(income_accounts):
        if update.callback_query:
            await update.callback_query.answer()
        await update.effective_message.reply_text("تاب Income_Accounts في الشيت فاضي. اكتب فيه حسابات الإيرادات (المستوى1 | المستوى2 | المستوى3) من اليومية.")
        return ConversationHandler.END
    if update.callback_query:
        await update.callback_query.answer()
    L = await run(read_lists)
    context.user_data.clear()
    form = {"type": typ, "uid": uid, "L": L, "opts": pay_acc_options(L, typ in ("pay", "opay")), "vals": {"date": _today_str()}}
    if typ in ("exp", "inc"):
        form["tree"] = tree_of({"flow": typ})
    if typ == "buy":
        form["opts"] = form["opts"] + [CREDIT_OPT]
    form["vals"].update(preset or {})
    return await form_open(update, context, form)


async def prefilled_form(update, context, typ, meta, vals):
    """فورم للمحاسب ببيانات جاية من تحويل (م/ جمعه أو م/ أحمد) أو طلب أمين الخزنة. بيكمّل الباقي ويأكّد."""
    L = await run(read_lists)
    context.user_data.clear()
    form = {"type": "opay" if typ == "pay" else typ, "uid": update.effective_user.id, "L": L, "opts": pay_acc_options(L),
            "vals": {"date": _today_str(), **vals}, "meta": meta}
    if typ in ("exp", "inc"):
        form["tree"] = tree_of({"flow": typ})
    if typ == "buy":
        form["opts"] = form["opts"] + [CREDIT_OPT]
    return await form_open(update, context, form)


async def pay_form_photo(update, context):
    """بياع بعت صورة وعليها نقطة: الفورم يفتح والصورة مرفقة."""
    await ensure_roles()
    return await start_pay(update, context, att=att_from_msg(update.message))


def cash_uid_ok(uid):
    return uid in CASH_MAIN_IDS or uid in CASH_SUB_IDS


def cash_form_ok(uid):
    return cash_uid_ok(uid) or uid in ADMINS


async def cash_form_open(update, context, att=None):
    await ensure_roles()
    uid = update.effective_user.id
    if not cash_form_ok(uid):
        if update.callback_query:
            await update.callback_query.answer("الخيار ده لأمين الخزنة بس", show_alert=True)
        return ConversationHandler.END
    if update.callback_query:
        await update.callback_query.answer()
    context.user_data.clear()
    admin = uid in ADMINS and not cash_uid_ok(uid)
    name = ADMIN_USERS.get(uid) if admin else None
    name = name or (await run(find_user, uid))[0] or update.effective_user.full_name
    form = {"type": "cash", "uid": uid, "name": name, "opts": [], "L": {}, "vals": {"date": _today_str(), "att": att},
            "admin": admin, "native": admin}
    return await form_open(update, context, form)


async def cash_form_photo(update, context):
    return await cash_form_open(update, context, att=att_from_msg(update.message))


class _FormPhoto(filters.MessageFilter):
    """صورة وعليها نقطة (.) بس: بتفتح الفورم. للبياع فورم الدفعة، ولأمين الخزنة فورم الخزنة."""

    def __init__(self, cash):
        super().__init__()
        self.cash = cash

    def filter(self, message):
        u = message.from_user
        has_img = bool(message.photo) or bool(message.document and (message.document.mime_type or "").startswith("image/"))
        if not u or not has_img or (message.caption or "").strip() != ".":
            return False
        if self.cash:
            return cash_uid_ok(u.id)
        return not cash_uid_ok(u.id) and u.id not in ADMINS and u.id not in CEO_IDS and u.id != GOMAA_ID


def form_parse(key, raw):
    """(نجح؟, القيمة, رسالة الخطأ)"""
    raw = raw.strip()
    if not raw:
        return False, None, "اكتب قيمة."
    if key == "amount":
        try:
            a = float(raw.translate(AR_DIGITS).replace(",", ""))
            assert a > 0
            return True, a, ""
        except Exception:
            return False, None, "المبلغ غلط. اكتب أرقام بس، مثلًا 25000:"
    if key == "date":
        try:
            return True, datetime.strptime(raw.translate(AR_DIGITS), "%d/%m/%Y").strftime("%Y-%m-%d"), ""
        except ValueError:
            return False, None, "التاريخ غلط. اكتبه كده: 25/10/2026"
    if key == "voucher":
        return True, raw.translate(AR_DIGITS), ""
    return True, raw, ""


def form_apply_text(form, raw):
    """بيوزّع الرسالة على الحقول: سطر "الاسم: القيمة" أو قيم بالترتيب على الحقول الفاضية. بيرجّع (أخطاء, محتاج اختيار موظف؟)."""
    fields = [f for f in form_fields(form) if f[2] in ("text", "num", "date", "emp")]
    v = form["vals"]
    errs, choose = [], False
    pending = [f for f in fields if not v.get(f[0])]
    for line in [l.strip() for l in raw.replace("：", ":").splitlines() if l.strip()]:
        target, val = None, line
        if ":" in line:
            lab, rest = line.split(":", 1)
            nl = norm(lab)
            if nl:
                target = next((f for f in fields if norm(f[1]) == nl), None) or \
                    next((f for f in fields if len(nl) >= 2 and (nl in norm(f[1]) or norm(f[1]) in nl)), None)
            if target:
                val = rest.strip()
        if not target:
            target = next((f for f in pending if f[0] != "date"), None)
        if not target:
            errs.append(f"مش لاقي حقل فاضي لـ: {line}")
            continue
        key, label = target[0], target[1]
        if target in pending:
            pending.remove(target)
        if not val:
            continue
        if key == "emp":
            q = norm(val)
            matches = [e for e in EMPLOYEES if q in norm(e)][:10]
            if not matches:
                errs.append(f"{label}: مفيش موظف بالاسم ده")
            elif len(matches) == 1:
                v["emp"] = matches[0]
            else:
                form["emp_matches"], choose = matches, True
            continue
        ok, parsed, err = form_parse(key, val)
        if ok:
            v[key] = parsed
        else:
            errs.append(f"{label}: {err}")
    return errs, choose


async def form_text_in(update, context):
    form = context.user_data.get("form")
    if not form:
        return ConversationHandler.END
    raw = update.message.text
    try:
        await update.message.delete()
    except Exception:
        pass
    if raw.strip() in (TZ_SEARCH_BTN, TZ_INV_BTN):
        context.user_data.pop("form", None)
        return await (tz_search_start if raw.strip() == TZ_SEARCH_BTN else tz_inv_open)(update, context)
    if form.get("mode") in ("tree", "paste") or form["type"] in ("tsearch", "tcount"):
        r = await gen_text(update, context, form, raw)
        if r is not None:
            return r
    key = form.get("await")
    if key in ("other_bank", "other_bank2", "date"):
        if key == "date":
            ok, parsed, err = form_parse("date", raw)
            if not ok:
                await context.bot.send_message(form["chat"], "⚠️ " + err)
                return F_TEXT
            form["vals"]["date"] = parsed
        elif raw.strip():
            form["vals"][key] = raw.strip()
        form.update(mode="edit", **{"await": None})
        await form_refresh(context, form)
        return F_VIEW
    errs, choose = form_apply_text(form, raw)
    if choose:
        form.update(mode="choice", cur="emp")
    await form_refresh(context, form)
    if errs:
        await context.bot.send_message(form["chat"], "⚠️ " + "\n".join(errs))
    return F_VIEW


async def form_photo_in(update, context):
    form = context.user_data.get("form")
    if not form:
        return ConversationHandler.END
    a = att_from_msg(update.message)
    if not a:
        return F_TEXT if form.get("await") else F_VIEW
    form["vals"]["att"] = a
    if form.get("await") == "att":
        form.update(mode="edit", **{"await": None})
    await form_refresh(context, form)
    return F_TEXT if form.get("await") else F_VIEW


async def form_choose(update, context, form, key, i):
    q = update.callback_query
    v = form["vals"]
    if key in ("acc", "acc2"):
        kind, method, _ = form["opts"][i]
        ok_key = "other_bank" if key == "acc" else "other_bank2"
        v[key] = {"kind": kind, "method": method}
        v.pop(ok_key, None)
        if method.startswith("أخرى"):
            await q.answer()
            form.update(mode="ask", **{"await": ok_key})
            await form_refresh(context, form)
            return F_TEXT
    else:
        val = form_options(form, key)[i]
        if key == "stype" and val not in SUP_DONE:
            await q.answer(f"قيد {val} لسه مش متحدد، فماتسجلش حاجة. ارجع لمسؤول الحسابات. 🚫", show_alert=True)
            return F_VIEW
        if key == "ctype":
            if val in CUSTODY_LABELS.values() and next(c for c in CUSTODY_TYPES if CUSTODY_LABELS[c] == val) not in CUS_DONE:
                await q.answer(f"قيد {val} لسه مش متحدد، فماتسجلش حاجة. ارجع لمسؤول الحسابات. 🚫", show_alert=True)
                return F_VIEW
            if val == CTYPE_LABELS[-1]:        # تسوية عهدة: لسه بخطوات
                await q.answer()
                context.user_data.pop("form", None)
                context.user_data["sL"] = form["L"]
                context.user_data["sd"] = {"uid": q.from_user.id, "flow": "cus", "method": "", "other_bank": "", "phase": "from"}
                context.user_data["shist"] = []
                return await sgo(update, context, S_STL)
        if key == "emp":
            val = form["emp_matches"][i]
        v[key] = val
        if key == "l1":
            v.pop("l2", None)
            v.pop("l3", None)
            v.pop("l4", None)
        elif key == "l2":
            v.pop("l3", None)
            v.pop("l4", None)
        elif key == "l3":
            v.pop("l4", None)
        elif key == "aclass":
            v.pop("aitem", None)
    await q.answer()
    form.update(mode="edit", **{"await": None})
    await form_refresh(context, form)
    return F_VIEW


async def form_cb(update, context):
    q = update.callback_query
    form = context.user_data.get("form")
    if not form:
        await q.answer("الفورم خلصت. ابعت . من جديد", show_alert=True)
        return ConversationHandler.END
    parts = q.data.split(":")
    act = parts[1]
    v = form["vals"]
    if act in ("tk", "tu", "tok", "tp", "tw", "paste", "pgo") or \
            (act == "o" and any(f[0] == parts[2] and f[2] == "tree" for f in form_fields(form))):
        return await gen_cb(update, context, form, parts)
    if act == "n":
        await q.answer()
        return F_TEXT if form.get("await") else F_VIEW
    if act == "c":
        context.user_data.pop("form", None)
        await q.answer()
        meta = form.get("meta") or {}
        if meta.get("tq") and await tq_back_to_menu(update, meta["tq"]):
            return ConversationHandler.END
        if meta.get("cx") and await cx_back_menu(update, meta["cx"]):
            return ConversationHandler.END
        if update.effective_user.id in ADMINS:
            await say(update, "اتلغى. تفتح إيه؟", menu_markup())
        else:
            await say(update, "اتلغى. ابعت . لو عايز تبدأ من جديد.")
        return ConversationHandler.END
    if act == "s":
        return await form_send(update, context, form)
    if act == "v":
        return await form_choose(update, context, form, form["cur"], int(parts[2]))
    if act == "p":
        return await form_choose(update, context, form, parts[2], int(parts[3]))
    if act == "r":
        miss = form_missing(form)
        if miss:
            await q.answer("لسه ناقص: " + "، ".join(miss), show_alert=True)
            return F_VIEW
        await q.answer()
        form.update(mode="review", **{"await": None})
        await form_refresh(context, form)
        return F_VIEW
    await q.answer()
    if act == "o":
        if not form_options(form, parts[2]):
            await q.answer("اختار الحقل اللي قبله الأول", show_alert=True)
            return F_VIEW
        form.update(mode="choice", cur=parts[2])
        await form_refresh(context, form)
        return F_VIEW
    if act == "d":
        if parts[2] == "x":
            form.update(mode="ask", **{"await": "date"})
            await form_refresh(context, form)
            return F_TEXT
        v["date"] = _days_ago(0)
    form.update(mode="edit", **{"await": None})
    await form_refresh(context, form)
    return F_VIEW


def form_build_sd(form):
    """بيحوّل الفورم لنفس البيانات اللي فلو الخطوات كان بيبنيها، عشان التسجيل يفضل بنفس القيود."""
    t, v = form["type"], form["vals"]
    d = {"uid": form["uid"], "amount": v["amount"], "date": v["date"], "att": v.get("att"),
         "method": "", "other_bank": "", "phase": "to"}

    def acc(key="acc", ok="other_bank"):
        a = v[key]
        return a["kind"], a["method"], v.get(ok, "")
    if t == "sup":
        d.update(flow="sup", supplier=v["supplier"], stype=v["stype"])
        if v["stype"] == SUPPLIER_TYPES[0]:
            d["kind"], d["method"], d["other_bank"] = acc()
    elif t in ("exp", "inc"):
        d.update(flow=t, l1x=v["l1"], l2x=v["l2"], l3x=v.get("l3", ""), l4x=v.get("l4", ""), note=v.get("note", ""))
        d["kind"], d["method"], d["other_bank"] = acc()
    elif t == "cus":
        d["emp"] = v["emp"] if v["emp"] in EMPLOYEES else next((e for e in EMPLOYEES if e.strip() == v["emp"]), v["emp"])
        d["kind"], d["method"], d["other_bank"] = acc()
        if v["ctype"] == "رد عهدة":
            d["flow"] = "cret"
        else:
            d.update(flow="cus", ctype=next(c for c in CUSTODY_TYPES if CUSTODY_LABELS[c] == v["ctype"]))
    elif t == "adv":
        d["emp"] = next((e for e in EMPLOYEES if e.strip() == v["emp"]), v["emp"])
        d["flow"] = "adv" if v["atype"] == "سلفة" else "aret"
        d["kind"], d["method"], d["other_bank"] = acc()
    elif t == "buy":
        d.update(flow="buy", aclass=v["aclass"], aitem=v.get("aitem", ""), note=v.get("note", ""))
        d["kind"], d["method"], d["other_bank"] = acc()
        if d["kind"] == "credit":
            d["supplier"] = v["supplier"]
    elif t == "sale":
        d.update(flow="sale", seller=v["seller"], client=v["client"], project=v["project"])
    elif t == "xfer":
        d["flow"] = "xfer"
        sk, sm, so = acc()
        d["src"], d["src_cash"] = (sm, so), sk == "cash"
        d["kind"], d["method"], d["other_bank"] = acc("acc2", "other_bank2")
    if v.get("voucher"):
        d["voucher"] = v["voucher"]
    meta = form.get("meta") or {}
    d.update({k: meta[k] for k in ("tq", "cx", "submitter") if meta.get(k)})
    if meta.get("note") and not d.get("note"):
        d["note"] = meta["note"]
    return d


async def form_send(update, context, form):
    q = update.callback_query
    miss = form_missing(form)
    if miss:
        if q:
            await q.answer("لسه ناقص: " + "، ".join(miss), show_alert=True)
        else:
            await update.effective_message.reply_text("⚠️ لسه ناقص: " + "، ".join(miss))
        return F_VIEW
    if q:
        await q.answer()
    v = form["vals"]
    t = form["type"]
    if t == "gen":
        return await gen_send(update, context, form)
    if t == "tcount":
        return await tcount_send(update, context, form)
    if t in ("pay", "opay"):
        acc = v["acc"]
        context.user_data["d"] = {
            "uid": form["uid"], "seller": v.get("seller") or form["seller"], "client": v["client"], "ptype": v["ptype"],
            "amount": v["amount"], "kind": acc["kind"], "method": acc["method"], "other_bank": v.get("other_bank", ""),
            "date": v["date"], "notes": v.get("notes", ""), "att": v.get("att"), "project": v.get("project", ""),
            "from_form": True,
            "voucher": v.get("voucher", "") if acc["kind"] == "cash" else ""}
        meta = form.get("meta") or {}
        context.user_data["d"].update({k: meta[k] for k in ("tq", "cx", "submitter") if meta.get(k)})
        context.user_data.pop("form", None)
        return await send_request(update, context)
    if t == "cash":
        d = {"kind": "inc" if v["kind"] == "إيراد" else "exp",
             "sub": (v.get("tsub") == "الخزنة الفرعية") if form.get("admin") else (form["uid"] in CASH_SUB_IDS and form["uid"] not in CASH_MAIN_IDS),
             "bayan": v["bayan"], "amount": v["amount"], "date": v["date"], "party": v["party"], "voucher": v["voucher"], "att": v.get("att")}
        context.user_data.pop("form", None)
        await say(update, await cm_submit(context, d, form["uid"], form["name"]), cash_again_kb())
        return ConversationHandler.END
    d = form_build_sd(form)
    L = form["L"]
    context.user_data.pop("form", None)
    context.user_data["sd"] = d
    context.user_data["sL"] = L
    return await sup_confirm(update, context)


# ================= فورم المستجدات والتقارير (م/ أحمد وم/ جمعه) =================
CEO_BTNS = ["💧 فورم السيولة", "📰 فورم المستجدات", "📊 فورم التقارير"]
GM_BTNS = ["🧾 فورم العهد", "📊 فورم التقارير"]
REPORT_URL_LIMIT = 2600


def role_kb(btns):
    rows = [[KeyboardButton(b)] for b in btns]
    try:
        return ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True)
    except TypeError:
        return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def reports_base():
    base = MINIAPP_URL.split("?")[0]
    if base.endswith(".html"):
        base = base.rsplit("/", 1)[0] + "/"
    elif not base.endswith("/"):
        base += "/"
    return base + "reports.html"


def _short(name, n=26):
    t = str(name or "بدون اسم").replace("_", " ").strip()
    return t if len(t) <= n else t[:n - 1] + "…"


def _top(d, n):
    return [[_short(k), int(round(v))] for k, v in sorted(d.items(), key=lambda kv: -abs(kv[1]))[:n] if abs(v) >= 0.5]


def _cairo_now():
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Africa/Cairo"))
    except Exception:
        return datetime.now()


def _figures(rows, day):
    acc_in, acc_out, coll, supp, exp, sales = {}, {}, 0.0, 0.0, 0.0, 0.0
    for r in rows:
        dt = parse_date(r[1])
        if not dt or dt.date() != day:
            continue
        l1, l2, l3, l4 = r[4].strip(), r[5].strip(), r[6].strip() or "بدون اسم", r[7].strip()
        dr, cr = num(r[15]), num(r[16])
        if l2 in ("النقدية_بالخزينة", "النقدية_بالبنوك"):
            if dr:
                acc_in[l3] = acc_in.get(l3, 0) + dr
            if cr:
                acc_out[l3] = acc_out.get(l3, 0) + cr
        if l2 == "العملاء" and " تبع " in r[3]:
            coll += cr
        if l2 == "الموردين" and l4 == "الدفعات":
            supp += dr
        if l1 in EXP_L1:
            exp += dr - cr
        if l2 == "الإيرادات_غير_المحققة" and l3 == "قيمة المبيعات":
            sales += cr - dr
    return acc_in, acc_out, coll, supp, exp, sales


def data_news(n):
    from datetime import timedelta
    _GL_CACHE["t"] = 0
    rows = read_gl()
    today = _cairo_now().date()
    a_in, a_out, c, sp, ex, sl = _figures(rows, today)
    y_in, y_out, yc, ysp, yex, ysl = _figures(rows, today - timedelta(days=1))
    r0 = lambda v: int(round(v))
    notes = []
    try:
        notes = [[r[0], _short(r[1], 160)] for r in notes_read()[-3:]][::-1]
    except Exception as e:
        logging.warning("notes failed: %s", e)
    return {"k": "news", "d": today.strftime("%Y-%m-%d"),
            "m": [["تحصيلات العملاء", r0(c), r0(yc)], ["المبيعات المسجلة", r0(sl), r0(ysl)],
                  ["دفعات الموردين", r0(sp), r0(ysp)], ["المصروفات", r0(ex), r0(yex)],
                  ["دخل الحسابات", r0(sum(a_in.values())), r0(sum(y_in.values()))],
                  ["خرج من الحسابات", r0(sum(a_out.values())), r0(sum(y_out.values()))]],
            "in": _top(a_in, n), "out": _top(a_out, n), "notes": notes}


def data_reports(n):
    _GL_CACHE["t"] = 0
    rows = read_gl()
    cash = balances(rows, "النقدية_بالخزينة")
    bank_all = balances(rows, "النقدية_بالبنوك")
    wallets, banks = {}, {}
    for k, v in bank_all.items():
        (wallets if is_wallet(k) else banks)[k] = v
    backup = {k: v for k, v in wallets.items() if "احتياط" in norm(k)}
    main_w = {k: v for k, v in wallets.items() if k not in backup}
    total = sum(cash.values()) + sum(bank_all.values())
    allacc = {**cash, **banks, **main_w, **backup}
    now = datetime.now()
    py, pm = (now.year, now.month - 1) if now.month > 1 else (now.year - 1, 12)

    def in_month(r, y, m):
        dt = parse_date(r[1])
        return bool(dt and dt.year == y and dt.month == m)
    exp_cur, exp_prev, sal = {}, {}, [0.0, 0.0]
    for r in rows:
        for tgt, (y, m) in ((0, (now.year, now.month)), (1, (py, pm))):
            if not in_month(r, y, m):
                continue
            if r[4].strip() in EXP_L1:
                d = exp_cur if tgt == 0 else exp_prev
                d[r[5].strip()] = d.get(r[5].strip(), 0) + num(r[15]) - num(r[16])
            if r[5].strip() == "الإيرادات_غير_المحققة" and r[6].strip() == "قيمة المبيعات":
                sal[tgt] += num(r[16]) - num(r[15])
    top_exp = sorted(exp_cur.items(), key=lambda kv: -kv[1])[:n]
    cli = {k: v for k, v in balances(rows, "العملاء").items() if v > 0}
    sup = {k: v for k, v in balances(rows, "الموردين", credit_normal=True).items() if v > 0}
    return {"k": "rep", "u": _cairo_now().strftime("%Y-%m-%d %H:%M"),
            "liq": {"total": int(round(total)),
                    "g": [["الخزن", int(round(sum(cash.values())))], ["البنوك", int(round(sum(banks.values())))],
                          ["فودافون كاش", int(round(sum(main_w.values())))], ["المحفظة الاحتياطية", int(round(sum(backup.values())))]],
                    "it": _top({k: v for k, v in allacc.items() if v > 0}, n)},
            "sal": [int(round(sal[0])), int(round(sal[1]))],
            "exp": [int(round(sum(exp_cur.values()))), int(round(sum(exp_prev.values()))),
                    [[_short(k), int(round(v)), int(round(exp_prev.get(k, 0)))] for k, v in top_exp if v >= 0.5]],
            "cli": [int(round(sum(cli.values()))), len(cli), _top(cli, n)],
            "sup": [int(round(sum(sup.values()))), len(sup), _top(sup, n)]}


def data_gcus(n):
    _GL_CACHE["t"] = 0
    me = norm(CUSTODY_PERSON)
    got = spent = 0.0
    for r in read_gl():
        if r[5].strip() == CUSTODY_L2 and r[6].strip() in CUSTODY_TYPES and norm(r[7]) == me:
            got += num(r[15])
            spent += num(r[16])
    return {"k": "gcus", "u": _cairo_now().strftime("%Y-%m-%d %H:%M"),
            "got": int(round(got)), "spent": int(round(spent)), "bal": int(round(got - spent))}


def data_grep(n, role=None):
    _GL_CACHE["t"] = 0
    own = {"gm": norm(CUSTODY_PERSON), "mh": norm(SETTLE_EMP)}.get(role)
    pay, stl, tp, ts = {}, {}, 0.0, 0.0
    for r in read_gl():
        desc, cr = r[3].strip(), num(r[16])
        if cr <= 0:
            continue
        if r[5].strip() == "العملاء" and " تبع " in desc:
            who = r[12].strip() or desc.split(" تبع ")[-1].strip() or "بدون اسم"
            pay[who] = pay.get(who, 0) + cr
            tp += cr
        elif r[5].strip() == CUSTODY_L2 and r[6].strip() == "العهد_المؤقتة" and desc.startswith("تسوية عهدة"):
            if own and norm(r[7]) != own:
                continue
            who = r[7].strip() or "بدون اسم"
            stl[who] = stl.get(who, 0) + cr
            ts += cr
    return {"k": "grep", "u": _cairo_now().strftime("%Y-%m-%d %H:%M"),
            "tp": int(round(tp)), "ts": int(round(ts)), "pay": _top(pay, n), "stl": _top(stl, n)}


def pack_report_url(builder):
    """بيبني رابط التقرير ببيانات طازة جواه. لو الرابط طويل بيقلل عدد البنود لحد ما يدخل."""
    url = ""
    for n in (10, 7, 5, 3):
        obj = builder(n)
        raw = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        plain = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
        co = zlib.compressobj(9, zlib.DEFLATED, -15)
        zipped = base64.urlsafe_b64encode(co.compress(raw) + co.flush()).decode("ascii").rstrip("=")
        url = reports_base() + "?" + (urlencode({"d": plain, "z": 0}) if len(plain) <= 1500 else urlencode({"d": zipped, "z": 1}))
        if len(url) <= REPORT_URL_LIMIT:
            break
    return url


MH_BTNS = ["🧾 فورم العهدة الشخصية", "📊 فورم التقارير"]
ALL_ROLE_BTNS = list(dict.fromkeys(CEO_BTNS + GM_BTNS + MH_BTNS))
# نوع التقرير: (دالة الداتا، العنوان)
def data_liq(n):
    """السيولة لوحدها: نفس أرقام الموقف النقدي في التقارير."""
    r = data_reports(n)
    return {"k": "liq", "u": r["u"], "total": r["liq"]["total"], "g": r["liq"]["g"], "it": r["liq"]["it"]}


def liq_base():
    return reports_base().rsplit("/", 1)[0] + "/liquidity.html"


def pack_liq_url(builder):
    url = ""
    for n in (40, 25, 15, 10, 5, 3):
        raw = json.dumps(builder(n), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        url = liq_base() + "?" + urlencode({"d": base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")})
        if len(url) <= REPORT_URL_LIMIT:
            break
    return url


RV_KINDS = {"liq": (data_liq, "السيولة"), "news": (data_news, "مستجدات اليوم"), "rep": (data_reports, "التقارير"),
            "gcus": (data_gcus, "العهدة"), "grep": (data_grep, "التقارير"), "mcus": (None, "العهدة الشخصية")}


def mh_calc():
    """عهدة م/ محمد حسن من اليومية: المستلم (مدين) والمنصرف (دائن) وحسب كل بياع."""
    _GL_CACHE["t"] = 0
    me = norm(SETTLE_EMP)
    got = spent = other = 0.0
    by, cnt = {}, {}
    for r in read_gl():
        if r[5].strip() != CUSTODY_L2 or r[6].strip() not in CUSTODY_TYPES or norm(r[7]) != me:
            continue
        got += num(r[15])
        cr = num(r[16])
        if cr <= 0:
            continue
        spent += cr
        desc = r[3].strip()
        parts = [p.strip() for p in desc.split(" - ")]
        if desc.startswith("تسوية عهدة") and len(parts) >= 3 and parts[2]:
            by[parts[2]] = by.get(parts[2], 0) + cr
            cnt[parts[2]] = cnt.get(parts[2], 0) + 1
        else:
            other += cr
    return got, spent, other, by, cnt


def data_mcus(n):
    got, spent, other, by, cnt = mh_calc()
    items = [[_short(f"{k} ({cnt[k]})"), int(round(v))] for k, v in sorted(by.items(), key=lambda kv: -kv[1])[:n]]
    return {"k": "mcus", "u": _cairo_now().strftime("%Y-%m-%d %H:%M"), "got": int(round(got)), "spent": int(round(spent)),
            "bal": int(round(got - spent)), "oth": int(round(other)), "by": items}


def mh_custody_text():
    got, spent, other, by, cnt = mh_calc()
    bal = got - spent
    lines = [rep_item(f"{k} ({cnt[k]} حركة)", v) for k, v in sorted(by.items(), key=lambda kv: -kv[1])] or ["▫️ مفيش"]
    status = (f"💼 <b>لسه معاك عهدة: {money(bal)} ج</b>" if bal > 0.5 else
              (f"⚠️ <b>المنصرف أكبر من المستلم بـ {money(-bal)} ج</b>" if bal < -0.5 else "✅ <b>العهدة متسوّية بالكامل</b>"))
    return "\n".join([rep_head("🧾", "العهدة الشخصية"), "",
                      f"📥 إجمالي المستلم: <b>{money(got)} ج</b>", f"📤 إجمالي المنصرف: <b>{money(spent)} ج</b>", status, "", LINE,
                      "👥 <b>المنصرف حسب البياع</b>"] + lines
                     + ([f"▫️ رد عهدة وحاجات تانية: <b>{money(other)}</b> ج"] if other > 0.5 else []))


async def role_of(uid):
    await ensure_roles()
    if uid in ADMINS:
        return "ad"
    if uid in CEO_IDS:
        return "ceo"
    if GOMAA_ID and uid == GOMAA_ID:
        return "gm"
    try:
        name, role = await run(find_user, uid)
    except Exception:
        return None
    return "mh" if role == "sales_mgr" else None


def kind_for(who, txt):
    table = {"ceo": {CEO_BTNS[0]: "liq", CEO_BTNS[1]: "news", CEO_BTNS[2]: "rep"}, "ad": {CEO_BTNS[1]: "news", CEO_BTNS[2]: "rep"},
             "gm": {GM_BTNS[0]: "gcus", GM_BTNS[1]: "grep"},
             "mh": {MH_BTNS[0]: "mcus", MH_BTNS[1]: "grep"}}
    return table.get(who, {}).get(txt)


def builder_for(kind, role=None):
    if kind == "grep":
        return lambda n: data_grep(n, role)
    return data_mcus if kind == "mcus" else RV_KINDS[kind][0]


async def role_form_btn(update, context):
    """الزرار الثابت: بيجهّز آخر الأرقام ويسيب لك تختار: فورم ولا عرض عادي."""
    if not MINIAPP_URL:
        return
    uid = update.effective_user.id
    who = await role_of(uid)
    kind = kind_for(who, (update.message.text or "").strip())
    if not kind:
        return
    try:
        url = await run(pack_liq_url if kind == "liq" else pack_report_url, builder_for(kind, who))
    except Exception as e:
        logging.warning("report form failed: %s", e)
        await update.message.reply_text("مقدرتش أجهّز الفورم دلوقتي. جرّب تاني بعد شوية.")
        raise ApplicationHandlerStop
    await update.message.reply_text(
        f"✅ {RV_KINDS[kind][1]} جاهزة بآخر الأرقام 👇",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📊 افتح الفورم", web_app=WebAppInfo(url=url))],
                                           [InlineKeyboardButton("📋 عرض عادي", callback_data=f"rv:{kind}")]]))
    raise ApplicationHandlerStop


async def rv_cb(update, context):
    """العرض العادي (نفس التقارير القديمة بالزراير والنصوص)."""
    q = update.callback_query
    uid = q.from_user.id
    kind = q.data.split(":")[1]
    who = await role_of(uid)
    allowed = {"ceo": ("liq", "news", "rep"), "gm": ("gcus", "grep"), "mh": ("mcus", "grep"),
               "ad": ("liq", "news", "rep")}.get(who, ())
    if kind not in allowed:
        await q.answer("غير مسموح", show_alert=True)
        return
    await q.answer()
    if kind == "rep":
        await q.message.reply_text("حضرتك تختار أنهي تقرير؟", reply_markup=ceo_reports_markup(uid))
        return
    markup = None
    if kind == "liq":
        text = await run(lambda: report_liquidity(read_gl()))
    elif kind == "news":
        text = await run(news_report)
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("📝 الملاحظات", callback_data="ceo:notes")]])
    elif kind == "gcus":
        text = await run(gomaa_custody_report)
    elif kind == "mcus":
        text = await run(mh_custody_text)
    else:
        text = await run(gomaa_report, who)
    await q.message.reply_text(cut_text(text), parse_mode="HTML", reply_markup=markup)


ADMIN_MODE = {}      # {uid: "n" عادي | "f" فورم}، بيتحدد من أول رسالة بعد الترحيب


def admin_form_kb():
    rows = [[KeyboardButton(CEO_BTNS[1])], [KeyboardButton(CEO_BTNS[2])], [KeyboardButton(MENU_BTN)]]
    try:
        return ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True)
    except TypeError:
        return ReplyKeyboardMarkup(rows, resize_keyboard=True)


async def mode_cb(update, context):
    await ensure_roles()
    q = update.callback_query
    uid = q.from_user.id
    if uid not in ADMINS:
        await q.answer("للمحاسبين بس", show_alert=True)
        return
    await q.answer()
    mode = q.data.split(":")[1]
    ADMIN_MODE[uid] = mode
    await q.edit_message_text("تفتح إيه؟", reply_markup=menu_markup())
    if mode == "f":
        await q.message.reply_text("📝 الوضع: رسائل فورم. أي حاجة تفتحها هتظهرلك كفورم، والمستجدات والتقارير في الزراير اللي تحت.", reply_markup=admin_form_kb())
    else:
        await q.message.reply_text("📋 الوضع: رسائل عادية.", reply_markup=menu_kb())


def mh_kb(pay_url):
    rows = [[KeyboardButton("📝 فورم دفعة عميل", web_app=WebAppInfo(url=pay_url))], [KeyboardButton(MH_BTNS[0])], [KeyboardButton(MH_BTNS[1])]]
    try:
        return ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True)
    except TypeError:
        return ReplyKeyboardMarkup(rows, resize_keyboard=True)


# ================= Mini App: الفورم كلها في شاشة واحدة =================
MINI_PENDING = {}     # {uid: form} فورم اتبعتت من الصفحة وبتستنى صورة المرفق (اختياري)
MINI_ATT = {}         # {uid: att} صورة اتبعتت قبل فتح الفورم (نقطة + صورة)
MINI_META = {}        # {uid: meta} بيانات تحويل جاي من م/ أحمد أو م/ جمعه أو أمين الخزنة
MINI_TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "form_template.html")
MINI_OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "miniapp", "index.html")
REP_TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "report_template.html")
REP_OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "miniapp", "reports.html")
LIQ_OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "miniapp", "liquidity.html")
LIQ_HTML = '<!DOCTYPE html>\n<html lang="ar" dir="rtl"><head><meta charset="utf-8">\n<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n<title>السيولة</title>\n<script src="https://telegram.org/js/telegram-web-app.js"></script>\n<style>\n:root{--bg:#FBF6EE;--card:#fff;--ink:#1F1F1F;--or:#F2A65A;--or2:#FCE7CF;--mut:#7A7266}\n*{box-sizing:border-box}\nhtml,body{margin:0;background:var(--bg);color:var(--ink);font-family:"Segoe UI",Tahoma,Arial,sans-serif}\nbody{padding:14px 12px 28px}\nh1{font-size:19px;margin:2px 0 4px;text-align:center}\n.up{text-align:center;color:var(--mut);font-size:13px;margin-bottom:12px}\n.tot{background:var(--ink);color:#fff;border-radius:16px;padding:16px;text-align:center;margin-bottom:12px}\n.tot small{display:block;opacity:.8;font-size:14px}\n.tot b{font-size:30px;color:var(--or)}\n.grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:12px}\n.g{background:var(--or2);border:2px solid var(--or);border-radius:14px;padding:10px;text-align:center}\n.g small{display:block;font-size:13px;color:var(--mut)}\n.g b{font-size:18px}\n.card{background:var(--card);border-radius:14px;padding:6px 12px;box-shadow:0 1px 3px rgba(0,0,0,.08)}\n.card h2{font-size:15px;margin:8px 0}\n.r{display:flex;justify-content:space-between;gap:8px;padding:9px 0;border-top:1px solid #EFE7DA;font-size:15px}\n.r b{white-space:nowrap}\n.neg{color:#C0392B}\n</style></head><body>\n<h1>💧 السيولة</h1><div class="up" id="up"></div>\n<div class="tot"><small>إجمالي السيولة</small><b id="tot">—</b> <small>جنيه</small></div>\n<div class="grid" id="g"></div>\n<div class="card"><h2>الحسابات</h2><div id="it"></div></div>\n<script>\nconst tg=window.Telegram&&window.Telegram.WebApp; if(tg){tg.ready();tg.expand();}\nconst f=n=>Number(n).toLocaleString(\'en-US\');\nlet D={};\ntry{let p=new URLSearchParams(location.search).get(\'d\')||\'\';p=p.replace(/-/g,\'+\').replace(/_/g,\'/\');p+=\'=\'.repeat((4-p.length%4)%4);\nD=JSON.parse(new TextDecoder().decode(Uint8Array.from(atob(p),c=>c.charCodeAt(0))));}catch(e){}\ndocument.getElementById(\'up\').textContent=D.u?(\'آخر تحديث: \'+D.u):\'\';\ndocument.getElementById(\'tot\').textContent=D.total!==undefined?f(D.total):\'—\';\nconst g=document.getElementById(\'g\');\n(D.g||[]).forEach(x=>{const d=document.createElement(\'div\');d.className=\'g\';d.innerHTML=\'<small></small><b></b>\';d.children[0].textContent=x[0];d.children[1].textContent=f(x[1]);g.append(d);});\nconst it=document.getElementById(\'it\');\n(D.it||[]).forEach(x=>{const r=document.createElement(\'div\');r.className=\'r\';r.innerHTML=\'<span></span><b></b>\';r.children[0].textContent=x[0];r.children[1].textContent=f(x[1]);if(x[1]<0)r.children[1].className=\'neg\';it.append(r);});\nif(!(D.it||[]).length)it.textContent=\'مفيش بيانات\';\n</script></body></html>\n'


def mini_data_json():
    """كل القوايم اللي الصفحة محتاجاها، من الشيت ومن الكود."""
    L = read_lists()
    income_accounts()
    inc_l1, inc_l2, inc_l3 = tree_of({"flow": "inc"})
    opts = pay_acc_options(L)
    nums = {o[1]: WALLET_NUMBERS[wallet_label(o[1])] for o in opts if o[0] == "wallet" and wallet_label(o[1]) in WALLET_NUMBERS}
    return {
        "titles": FORM_TITLES, "types": L["types"], "opts": opts, "nums": nums, "sellers": L["sellers"],
        "suppliers": SUPPLIERS, "stypes": list(SUPPLIER_TYPES),
        "blocked": [x for x in SUPPLIER_TYPES if x not in SUP_DONE] + [CUSTODY_LABELS[c] for c in CUSTODY_TYPES if c not in CUS_DONE],
        "ctypes": [CTYPE_LABELS[0], CTYPE_LABELS[1], CTYPE_LABELS[-2], "تسوية عهدة"], "atypes": ATYPE_LABELS, "emps": [e.strip() for e in EMPLOYEES],
        "exp": {"l1": EXP_L1, "l2": EXP_TREE, "l3": EXP_L3},
        "inc": {"l1": inc_l1, "l2": inc_l2, "l3": inc_l3},
    }


def mini_build_html():
    with open(MINI_TEMPLATE, encoding="utf-8") as f:
        html = f.read()
    data = json.dumps(mini_data_json(), ensure_ascii=False)
    assert "/*DATA*/null" in html, "form_template.html مش سليم"
    html = html.replace("/*DATA*/null", data.replace("</", "<\\/"))
    os.makedirs(os.path.dirname(MINI_OUT), exist_ok=True)
    with open(MINI_OUT, "w", encoding="utf-8") as f:
        f.write(html)
    with open(REP_TEMPLATE, encoding="utf-8") as f:
        rep_html = f.read()
    with open(REP_OUT, "w", encoding="utf-8") as f:
        f.write(rep_html)
    with open(LIQ_OUT, "w", encoding="utf-8") as f:
        f.write(LIQ_HTML)
    with open(BAL_OUT, "w", encoding="utf-8") as f:
        f.write(BAL_HTML)
    return MINI_OUT


async def buildform_cmd(update, context):
    await ensure_roles()
    if update.effective_user.id not in ADMINS:
        return
    try:
        path = await run(mini_build_html)
    except Exception as e:
        await update.message.reply_text(f"مقدرتش أبني الصفحة: {e}\nتأكد إن ملف form_template.html جنب bot.py.")
        return
    with open(path, "rb") as f:
        await update.message.reply_document(f, filename="index.html", caption="1/3 ✅ ملف الفورم: index.html")
    with open(REP_OUT, "rb") as f:
        await update.message.reply_document(f, filename="reports.html", caption="2/3 ✅ ملف التقارير: reports.html")
    with open(LIQ_OUT, "rb") as f:
        await update.message.reply_document(f, filename="liquidity.html",
                                            caption="3/4 ✅ ملف السيولة: liquidity.html")
    with open(BAL_OUT, "rb") as f:
        await update.message.reply_document(f, filename="balances.html",
                                            caption="4/4 ✅ ملف الأرصدة: balances.html\nارفع الأربعة على GitHub (Add file ثم Upload files). لو غيّرت أي قايمة في الشيت ابعت /buildform تاني وارفع index.html بس.")


def mini_kb(title, url, menu=False, uid=None):
    btns = [[KeyboardButton("📝 " + title, web_app=WebAppInfo(url=url))]] + ([[KeyboardButton(MENU_BTN)]] if menu else [])
    if uid is not None and cash_uid_ok(uid):
        btns += [[KeyboardButton(TZ_SEARCH_BTN)], [KeyboardButton(TZ_INV_BTN)]]
    try:
        return ReplyKeyboardMarkup(btns, resize_keyboard=True, is_persistent=True)
    except TypeError:
        return ReplyKeyboardMarkup(btns, resize_keyboard=True)


def mini_url(form):
    params = {"f": form["type"]}
    if form["type"] == "pay":
        params["who"] = form.get("seller", "")
    pre = {k: v for k, v in (form.get("vals") or {}).items() if k not in ("att", "date") and v not in (None, "")}
    if pre:
        raw = json.dumps(pre, ensure_ascii=False).encode("utf-8")
        params["pre"] = base64.urlsafe_b64encode(raw).decode("ascii")
    sep = "&" if "?" in MINIAPP_URL else "?"
    return MINIAPP_URL + sep + urlencode(params)


async def mini_open(update, context, form):
    uid = form["uid"]
    title = FORM_TITLES[form["type"]]
    att = (form.get("vals") or {}).get("att")
    if att:
        MINI_ATT[uid] = att
    else:
        MINI_ATT.pop(uid, None)
    if form.get("meta"):
        MINI_META[uid] = form["meta"]
    else:
        MINI_META.pop(uid, None)
    MINI_PENDING.pop(uid, None)
    context.user_data.pop("form", None)
    await update.effective_message.reply_text(
        f"📝 {title}\nاضغط الزرار اللي تحت 👇 وهيفتحلك الفورم كلها في شاشة واحدة، املاها وابعت." + ("\nالصورة اللي بعتها هتتضاف معاها ✅" if att else ""),
        reply_markup=mini_kb(title, mini_url(form), menu=uid in ADMINS, uid=uid))
    return ConversationHandler.END


def mini_fill(form, inp):
    """بيحط اللي جاي من الصفحة في الفورم بعد ما يتأكد منه. بيرجّع قايمة أخطاء."""
    v, errs = form["vals"], []
    names = {f[0] for f in form_fields(form)}
    for key in ("seller", "supplier", "stype", "ctype", "atype", "kind", "l1", "l2", "l3", "l4", "ptype", "emp"):
        if key not in inp:
            continue
        names = {f[0] for f in form_fields(form)}
        if key not in names:
            continue
        val = str(inp[key]).strip()
        if key == "emp":
            hit = next((e for e in EMPLOYEES if e.strip() == val), None)
            if not hit:
                errs.append("الموظف مش في القايمة")
                continue
            v["emp"] = hit
            continue
        if val not in form_options(form, key):
            errs.append(f"اختيار غلط في: {val}")
            continue
        if key == "stype" and val not in SUP_DONE:
            errs.append(f"قيد {val} لسه مش متحدد، فماتسجلش حاجة. ارجع لمسؤول الحسابات.")
            continue
        if key == "ctype" and val in CUSTODY_LABELS.values() and next(c for c in CUSTODY_TYPES if CUSTODY_LABELS[c] == val) not in CUS_DONE:
            errs.append(f"قيد {val} لسه مش متحدد، فماتسجلش حاجة. ارجع لمسؤول الحسابات. 🚫")
            continue
        v[key] = val
        if key == "l1":
            v.pop("l2", None); v.pop("l3", None); v.pop("l4", None)
        elif key == "l2":
            v.pop("l3", None); v.pop("l4", None)
        elif key == "l3":
            v.pop("l4", None)
    for key, okey in (("acc", "other_bank"), ("acc2", "other_bank2")):
        a = inp.get(key)
        if not a or key not in {f[0] for f in form_fields(form)}:
            continue
        hit = next((o for o in form["opts"] if o[0] == a.get("kind") and o[1] == a.get("method")), None)
        if not hit:
            errs.append("حساب الدفع مش موجود")
            continue
        v[key] = {"kind": hit[0], "method": hit[1]}
        v.pop(okey, None)
        if hit[1].startswith("أخرى") and str(inp.get(okey, "")).strip():
            v[okey] = str(inp[okey]).strip()[:100]
    for key in ("client", "project", "bayan", "party", "voucher", "notes", "note", "amount", "date"):
        if key not in inp or key not in {f[0] for f in form_fields(form)}:
            continue
        raw = str(inp[key]).strip()
        if key == "date":
            try:
                v["date"] = datetime.strptime(raw, "%Y-%m-%d").strftime("%Y-%m-%d")
            except ValueError:
                errs.append("التاريخ غلط")
            continue
        ok, parsed, err = form_parse(key, raw[:300])
        if ok:
            v[key] = parsed
        else:
            errs.append(err)
    return errs


async def mini_data(update, context):
    """الصفحة بعتت الفورم مليانة: نتأكد منها وندخلها نفس مسار التسجيل القديم."""
    msg = update.effective_message
    uid = update.effective_user.id
    try:
        p = json.loads(msg.web_app_data.data)
        t, inp = p["type"], p["vals"]
        assert t in FORM_TITLES and t not in NATIVE_ONLY and isinstance(inp, dict)
    except Exception:
        await msg.reply_text("البيانات مش مفهومة. افتح الفورم تاني من الزرار اللي تحت.")
        raise ApplicationHandlerStop
    await ensure_roles()
    L = {}
    form = {"type": t, "uid": uid, "vals": {}, "opts": [], "L": L, "emp_matches": list(EMPLOYEES)}
    if t in ("pay", "opay"):
        if uid in ADMINS:
            t = form["type"] = "opay"
        else:
            name = (await run(read_seller_ids)).get(str(uid))
            if not name:
                await msg.reply_text("حسابك مش مفعّل للتحصيلات.")
                raise ApplicationHandlerStop
            t = form["type"] = "pay"
            form["seller"] = name
    elif t == "cash":
        if not cash_uid_ok(uid):
            await msg.reply_text("الفورم دي لأمين الخزنة بس.")
            raise ApplicationHandlerStop
        form["name"] = (await run(find_user, uid))[0] or update.effective_user.full_name
    elif uid not in ADMINS:
        await msg.reply_text("الفورم دي للمحاسبين بس.")
        raise ApplicationHandlerStop
    if t == "cus" and str(inp.get("ctype", "")).startswith("تسوية عهدة"):
        await msg.reply_text("تسوية العهدة بتتم بخطوات في الشات. اضغط الزرار وكمّل 👇",
                             reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("كمّل تسوية العهدة", callback_data="mp:stl")]]))
        raise ApplicationHandlerStop
    if t != "cash":
        L.update(await run(read_lists))
        form["opts"] = pay_acc_options(L, t in ("pay", "opay") and uid not in MINI_META)
    if t in ("exp", "inc"):
        if t == "inc":
            await run(income_accounts)
        form["tree"] = tree_of({"flow": t})
    errs = mini_fill(form, inp)
    miss = form_missing(form)
    if errs or miss:
        text = "⚠️ " + "\n".join(errs) if errs else ""
        if miss:
            text += ("\n" if text else "⚠️ ") + "لسه ناقص: " + "، ".join(miss)
        await msg.reply_text(text + "\nافتح الفورم تاني من الزرار اللي تحت وصلّحها.")
        raise ApplicationHandlerStop
    att = MINI_ATT.pop(uid, None)
    if att:
        form["vals"]["att"] = att
    meta = MINI_META.pop(uid, None)
    if meta:
        form["meta"] = meta
    context.user_data.clear()
    if att:
        await form_send(update, context, form)
        raise ApplicationHandlerStop
    MINI_PENDING[uid] = form
    await msg.reply_text("📎 عايز تضيف صورة (مرفق)؟ ابعتها دلوقتي. لو مفيش اضغط الزرار.",
                         reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("إرسال من غير صورة ✅", callback_data="mp:skip")]]))
    raise ApplicationHandlerStop


async def mini_stl_entry(update, context):
    """تسوية العهدة (بخطوات) اللي اختارها المحاسب من الصفحة."""
    await ensure_roles()
    q = update.callback_query
    if q.from_user.id not in ADMINS:
        await q.answer("للمحاسبين بس", show_alert=True)
        return ConversationHandler.END
    await q.answer()
    context.user_data.clear()
    context.user_data["sL"] = await run(read_lists)
    context.user_data["sd"] = {"uid": q.from_user.id, "flow": "cus", "method": "", "other_bank": "", "phase": "from"}
    context.user_data["shist"] = []
    return await sgo(update, context, S_STL)


async def mini_skip(update, context):
    q = update.callback_query
    form = MINI_PENDING.pop(q.from_user.id, None)
    if not form:
        await q.answer("الفورم خلصت. افتحها تاني.", show_alert=True)
        raise ApplicationHandlerStop
    context.user_data.clear()
    await form_send(update, context, form)
    raise ApplicationHandlerStop


class _MiniWaiting(filters.MessageFilter):
    def filter(self, message):
        u = message.from_user
        has_img = bool(message.photo) or bool(message.document and (message.document.mime_type or "").startswith("image/"))
        return bool(u and has_img and u.id in MINI_PENDING)


async def mini_photo(update, context):
    uid = update.effective_user.id
    form = MINI_PENDING.pop(uid, None)
    a = att_from_msg(update.message)
    if not form or not a:
        raise ApplicationHandlerStop
    form["vals"]["att"] = a
    context.user_data.clear()
    await form_send(update, context, form)
    raise ApplicationHandlerStop


# ================= رفض بسبب =================
REJ_PATTERN = r"^(no:\d+|tq:[^:]+:r|cx:[^:]+:r|rq:no:[^:]+)$"


async def rej_ask(update, context):
    """زرار رفض (في أي طلب): بيسأل عن السبب الأول، والسبب بيوصل لصاحب الطلب."""
    q = update.callback_query
    if q.from_user.id != OWNER_ID:
        await q.answer("الزرار ده للمدير بس", show_alert=True)
        raise ApplicationHandlerStop
    await q.answer()
    p = q.data.split(":")
    kind, key = ("pay", p[1]) if p[0] == "no" else ((p[0], p[1]) if p[0] in ("tq", "cx") else ("rq", p[2]))
    context.user_data["rej"] = {"kind": kind, "key": key, "chat": q.message.chat_id, "mid": q.message.message_id,
                                "text": q.message.text or ""}
    pm = await q.message.reply_text("اكتب سبب الرفض (هيوصل لصاحب الطلب):", reply_markup=InlineKeyboardMarkup([[
        InlineKeyboardButton("رفض بدون سبب", callback_data="rj:skip"), InlineKeyboardButton("إلغاء", callback_data="rj:cancel")]]))
    context.user_data["rej"]["pmsg"] = pm.message_id
    raise ApplicationHandlerStop


async def rej_do(context, rej, reason):
    why = f"\nالسبب: {reason}" if reason else ""
    kind, key = rej["kind"], rej["key"]
    gone = "الطلب ده اتعالج قبل كده (أو مش موجود)."
    if kind == "pay":
        ws, row, vals = await run(find_request, key)
        if not row or (len(vals) > 11 and vals[11].strip() != "معلّق"):
            return gone
        tg = vals[10] if len(vals) > 10 else ""
        await run(mark_rejected, key)
        notify = (rej.get("text") or f"طلبك رقم {key}") + "\n\n❌ اترفض." + why
    elif kind == "tq":
        ws, row, rec = await run(tq_find, key)
        if not row or rec["status"] != "مستني اعتماد":
            return gone
        await run(ws.update, f"J{row}:K{row}", [["مرفوض", reason]])
        tg, notify = rec["tg"], f"التحويل ({tq_ref(rec)}) اترفض.{why}"
    elif kind == "cx":
        ws, row, rec = await run(cx_find, key)
        if not row or rec["status"] != "مستني اعتماد":
            return gone
        await run(ws.update, f"K{row}:L{row}", [["مرفوض", reason]])
        tg, notify = rec["tg"], f"طلبك ({rec['kind']} {rec['amount']:,.2f}) اترفض.{why}"
    else:
        ws, row, vals = await run(rq_find, key)
        if not row:
            return gone
        d = rq_row_to_d(vals)
        if d["status"] in ("اتسجّل", "مرفوض"):
            return gone
        await run(ws.update, f"H{row}", [["مرفوض"]])
        tg, notify = d["tg"], f"تسوية العهدة بتاعتك ({d['amount']:,.2f}) اترفضت.{why}"
    try:
        await context.bot.edit_message_text(chat_id=rej["chat"], message_id=rej["mid"],
                                            text=rej["text"] + "\n\n❌ اترفض." + why)
    except Exception as e:
        logging.info("reject edit skipped: %s", e)
    if str(tg).strip().isdigit():
        try:
            await context.bot.send_message(int(str(tg).strip()), notify)
        except Exception:
            pass
    return "اترفض وبُلّغ صاحب الطلب ✅" + (" (مع السبب)" if reason else "")


async def rej_cb(update, context):
    q = update.callback_query
    if q.from_user.id != OWNER_ID:
        await q.answer("غير مسموح", show_alert=True)
        raise ApplicationHandlerStop
    await q.answer()
    rej = context.user_data.pop("rej", None)
    if not rej:
        await q.edit_message_text("مفيش رفض معلّق.")
    elif q.data.endswith("cancel"):
        await q.edit_message_text("اتلغى. الطلب لسه مستني.")
    else:
        res = await rej_do(context, rej, "")
        try:
            await q.message.delete()
        except Exception:
            await q.edit_message_text(res)
        else:
            if not res.startswith("اترفض"):
                await context.bot.send_message(q.message.chat_id, res)
    raise ApplicationHandlerStop


# ================= ملاحظات المدير (بتظهر لـ م/ أحمد في المستجدات > الملاحظات) =================
NOTES_TAB = "Manager_Notes"


def notes_read():
    ws, new = _tab(NOTES_TAB, 500, 3)
    if new:
        ws.update("A1", [["التاريخ", "الملاحظة", "وقت التسجيل"]])
    return [r for r in ws.get_all_values()[1:] if len(r) > 1 and r[1].strip()]


def notes_add(text):
    ws, new = _tab(NOTES_TAB, 500, 3)
    if new:
        ws.update("A1", [["التاريخ", "الملاحظة", "وقت التسجيل"]])
    _append(ws, [_today_str(), _safe_cell(text), datetime.now().strftime("%Y-%m-%d %H:%M:%S")])


def notes_clear():
    ws, _ = _tab(NOTES_TAB, 500, 3)
    ws.batch_clear([f"A2:C{ws.row_count}"])


def notes_text(rows, title):
    if not rows:
        return rep_head("📝", title) + "\n\nمفيش ملاحظات."
    body = "\n\n".join(f"🗓 <i>{esc(r[0])}</i>\n{esc(r[1])}" for r in reversed(rows[-15:]))
    return rep_head("📝", title) + "\n\n" + body


async def mn_cb(update, context):
    q = update.callback_query
    if q.from_user.id != OWNER_ID:
        await q.answer("للمدير بس", show_alert=True)
        return
    await q.answer()
    act = q.data.split(":")[1]
    if act == "home":
        await q.edit_message_text("تفتح إيه؟", reply_markup=menu_markup())
    elif act == "menu":
        context.user_data.pop("mn_wait", None)
        rows = await run(notes_read)
        await q.edit_message_text(cut_text(notes_text(rows, "ملاحظات المدير")), parse_mode="HTML", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ إضافة ملاحظة", callback_data="mn:add"), InlineKeyboardButton("🗑 مسح الكل", callback_data="mn:clr")],
            [InlineKeyboardButton("⬅️ رجوع", callback_data="mn:home")]]))
    elif act == "add":
        context.user_data["mn_wait"] = True
        await q.edit_message_text("اكتب الملاحظة (هتظهر لـ م/ أحمد في المستجدات ← الملاحظات، من غير ما تتبعتله):",
                                  reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("إلغاء", callback_data="mn:menu")]]))
    elif act == "clr":
        await q.edit_message_text("تمسح كل الملاحظات؟", reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("أيوه امسح", callback_data="mn:clr2"), InlineKeyboardButton("لأ", callback_data="mn:menu")]]))
    elif act == "clr2":
        await run(notes_clear)
        await q.edit_message_text("اتمسحت الملاحظات ✅", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ رجوع", callback_data="mn:menu")]]))


async def owner_text_hook(update, context):
    """نص من المحاسب: سبب رفض معلّق، أو ملاحظة مدير. غير كده بيعدّي للباقي."""
    rej = context.user_data.get("rej")
    txt = (update.message.text or "").strip()
    if context.user_data.get("bc_wait") and txt:
        await bc_got_text(update, context, txt)
        raise ApplicationHandlerStop
    if rej and txt:
        context.user_data.pop("rej", None)
        res = await rej_do(context, rej, txt)
        for mid in (rej.get("pmsg"), update.message.message_id):
            try:
                if mid:
                    await context.bot.delete_message(update.message.chat_id, mid)
            except Exception:
                pass
        if not res.startswith("اترفض"):
            await context.bot.send_message(update.message.chat_id, res)
        raise ApplicationHandlerStop
    if context.user_data.get("mn_wait") and txt:
        context.user_data.pop("mn_wait", None)
        await run(notes_add, txt)
        await update.message.reply_text("اتحفظت الملاحظة ✅", reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("📝 ملاحظات المدير", callback_data="mn:menu")]]))
        raise ApplicationHandlerStop


# ================= المستجدات (م/ أحمد): ملخص اليوم + الملاحظات =================
def news_report():
    _GL_CACHE["t"] = 0
    try:
        from zoneinfo import ZoneInfo
        today = datetime.now(ZoneInfo("Africa/Cairo")).date()
    except Exception:
        today = datetime.now().date()
    acc_in, acc_out = {}, {}
    coll = supp = exp = sales = 0.0
    for r in read_gl():
        dt = parse_date(r[1])
        if not dt or dt.date() != today:
            continue
        l1, l2, l3, l4 = r[4].strip(), r[5].strip(), r[6].strip() or "بدون اسم", r[7].strip()
        dr, cr = num(r[15]), num(r[16])
        if l2 in ("النقدية_بالخزينة", "النقدية_بالبنوك"):
            if dr:
                acc_in[l3] = acc_in.get(l3, 0) + dr
            if cr:
                acc_out[l3] = acc_out.get(l3, 0) + cr
        if l2 == "العملاء" and " تبع " in r[3]:
            coll += cr
        if l2 == "الموردين" and l4 == "الدفعات":
            supp += dr
        if l1 in EXP_L1:
            exp += dr - cr
        if l2 == "الإيرادات_غير_المحققة" and l3 == "قيمة المبيعات":
            sales += cr - dr

    def block(icon, title, d):
        body = "\n".join(rep_item(k, v) for k, v in sorted(d.items(), key=lambda kv: -kv[1])) or "▫️ مفيش"
        return f"{icon} <b>{title}</b>: <b>{money(sum(d.values()))}</b> ج\n{body}"
    return "\n".join([rep_head("📰", "مستجدات اليوم", today.strftime("%Y-%m-%d")), "",
                      block("📥", "اللي دخل الحسابات", acc_in), "", block("📤", "اللي خرج من الحسابات", acc_out), "", LINE, "",
                      f"👥 تحصيلات العملاء: <b>{money(coll)}</b> ج", f"🏭 دفعات الموردين: <b>{money(supp)}</b> ج",
                      f"📉 المصروفات: <b>{money(exp)}</b> ج", f"🧾 المبيعات المسجلة: <b>{money(sales)}</b> ج"])


async def ceo_cb(update, context):
    await ensure_roles()
    q = update.callback_query
    uid = q.from_user.id
    if uid not in CEO_IDS and uid != OWNER_ID:
        await q.answer("غير مسموح", show_alert=True)
        return
    await q.answer()
    act = q.data.split(":")[1]
    if act == "news":
        await q.edit_message_text(cut_text(await run(news_report)), parse_mode="HTML", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📝 الملاحظات", callback_data="ceo:notes")],
            [InlineKeyboardButton("⬅️ رجوع", callback_data="ceo:home")]]))
    elif act == "notes":
        rows = await run(notes_read)
        await q.edit_message_text(cut_text(notes_text(rows, "الملاحظات")), parse_mode="HTML", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ رجوع", callback_data="ceo:news")]]))
    else:
        if MINIAPP_URL:
            await q.edit_message_text("تمام 👍 اختار من الأزرار اللي تحت.")
            return
        await q.edit_message_text("حضرتك تختار إيه؟", reply_markup=ceo_menu(uid))


# ================= المعلّقات (للمحاسب): كل اللي مستني قرارك =================
def pending_report():
    out = [rep_head("📋", "المعلّقات (مستنية قرارك)"), ""]
    total = 0

    def sect(icon, title, items):
        nonlocal total
        total += len(items)
        lines = [f"▫️ {esc(i)}" for i in items[:8]] or ["▫️ مفيش"]
        more = [f"... و{len(items) - 8} غيرهم"] if len(items) > 8 else []
        out.extend([f"{icon} <b>{title}</b>: <b>{len(items)}</b>"] + lines + more + [""])
    rows = sheet.worksheet("Pending").get_all_values()[1:]
    sect("💰", "دفعات عملاء", [f"#{r[0]} | {r[6]} | {r[5]} | {num(r[4]):,.0f}" for r in rows if len(r) > 11 and r[11].strip() == "معلّق"])
    try:
        rows = sheet.worksheet(TQ_TAB).get_all_values()[1:]
        sect("🔁", "تحويلات (م/ جمعه وم/ أحمد)", [f"{r[0]} | {r[2]} | {r[5]}" for r in rows if len(r) > 9 and r[9].strip() == "مستني اعتماد"])
    except gspread.WorksheetNotFound:
        sect("🔁", "تحويلات (م/ جمعه وم/ أحمد)", [])
    try:
        rows = sheet.worksheet(CASH_PEND_TAB).get_all_values()[1:]
        sect("🏦", "طلبات الخزنة", [f"{r[0]} | {r[9]} | {r[6]} | {num(r[5]):,.0f}" for r in rows if len(r) > 10 and r[10].strip() == "مستني اعتماد"])
    except gspread.WorksheetNotFound:
        sect("🏦", "طلبات الخزنة", [])
    out.extend([LINE, f"✅ <b>الإجمالي: {total}</b>"])
    return "\n".join(out)


def pd_collect():
    """كل اللي مستني قرارك: دفعات عملاء + تحويلات + طلبات الخزنة (بتفاصيلها الكاملة)."""
    out = []
    try:
        for r in sheet.worksheet("Pending").get_all_values()[1:]:
            r = r + [""] * (17 - len(r))
            if r[11].strip() == "معلّق" and r[0].strip():
                out.append(("pay", r))
    except Exception as e:
        logging.warning("pending collect (pay) failed: %s", e)
    try:
        for r in sheet.worksheet(TQ_TAB).get_all_values()[1:]:
            r = r + [""] * (14 - len(r))
            if r[9].strip() == "مستني اعتماد" and r[0].strip():
                out.append(("tq", r))
    except gspread.WorksheetNotFound:
        pass
    except Exception as e:
        logging.warning("pending collect (tq) failed: %s", e)
    try:
        for r in sheet.worksheet(CASH_PEND_TAB).get_all_values()[1:]:
            r = r + [""] * (14 - len(r))
            if r[10].strip() == "مستني اعتماد" and r[0].strip():
                out.append(("cx", r))
    except gspread.WorksheetNotFound:
        pass
    except Exception as e:
        logging.warning("pending collect (cx) failed: %s", e)
    return out


async def pd_resend(context):
    """بيمسح رسايل المعلّقات القديمة ويبعتها كلها تاني في آخر الشات بنفس أزرارها."""
    items = await run(pd_collect)
    n = 0
    for kind, r in items:
        try:
            if kind == "pay":
                rid = r[0].strip()
                await msg_purge(context.bot, f"r{rid}")
                bank = f" ({r[8]})" if r[8] else ""
                try:
                    amt = f"{float(r[4]):,.2f}"
                except Exception:
                    amt = str(r[4])
                m = await context.bot.send_message(
                    OWNER_ID,
                    f"طلب جديد رقم {rid}\nالبياع: {r[6]}\nالعميل: {r[5]}\nالنوع: {nopct(r[3])}\n"
                    f"المبلغ: {amt}\nالدفع: {r[7]}{bank}\nالتاريخ: {r[2]}\nملاحظات: {r[9] or '-'}"
                    + (f"\nرقم الوصل: {r[15]}" if str(r[15]).strip() else "")
                    + (f"\nرقم المشروع: {r[16]}" if str(r[16]).strip() else "")
                    + ("\nالمرفق: ✅ (الصورة تحت)" if str(r[14]).startswith("tg:") else ""),
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("تأكيد", callback_data=f"ok:{rid}"),
                                                        InlineKeyboardButton("رفض", callback_data=f"no:{rid}")]]))
                msg_track(f"r{rid}", m)
                if str(r[14]).startswith("tg:"):
                    await send_att(context, OWNER_ID, str(r[14]), f"مرفق الطلب رقم {rid} ({r[6]})", key=f"r{rid}")
            elif kind == "tq":
                tid = r[0].strip()
                await msg_purge(context.bot, f"t{tid}")
                m = await context.bot.send_message(
                    OWNER_ID, f"📥 تحويل جديد من {r[2]}\nالبيان: {r[5]}\nالتاريخ: {r[7]}"
                    + ("\nالمرفق: ✅ (الصورة تحت)" if str(r[8]).startswith("tg:") else ""),
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("اعتماد وتحديد النوع", callback_data=f"tq:{tid}:s")],
                                                       [InlineKeyboardButton("رفض", callback_data=f"tq:{tid}:r")]]))
                msg_track(f"t{tid}", m)
                if str(r[8]).startswith("tg:"):
                    await send_att(context, OWNER_ID, str(r[8]), f"مرفق التحويل ({r[2]})", key=f"t{tid}")
            else:
                pid = r[0].strip()
                await msg_purge(context.bot, f"c{pid}")
                inc = r[6].strip() == "إيراد"
                m = await context.bot.send_message(
                    OWNER_ID,
                    f"📥 طلب جديد على {r[8]} من {r[9]}\nراجع الحركة:\nالنوع: {'إيراد' if inc else 'مصروف'}\nالبيان: {r[4]}\n"
                    f"المبلغ: {num(r[5]):,.2f}\nالتاريخ: {r[2]}\n{'اللي دفع' if inc else 'المستلم'}: {r[7]}\nرقم الوصل: {r[3]}"
                    + ("\nالمرفق: ✅" if str(r[12]).startswith("tg:") else ""),
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("اعتماد واختيار الحساب", callback_data=f"cx:{pid}:s")],
                        [InlineKeyboardButton("اعتماد من غير قيد", callback_data=f"cx:{pid}:n")],
                        [InlineKeyboardButton("رفض", callback_data=f"cx:{pid}:r")]]))
                msg_track(f"c{pid}", m)
                if str(r[12]).startswith("tg:"):
                    await send_att(context, OWNER_ID, str(r[12]), f"مرفق الطلب {pid} ({r[9]})", key=f"c{pid}")
            n += 1
        except Exception as e:
            logging.warning("pending resend failed for %s: %s", r[0] if r else "?", e)
        await asyncio.sleep(0.05)
    return n


async def pd_cb(update, context):
    q = update.callback_query
    if q.from_user.id != OWNER_ID:
        await q.answer("للمدير بس", show_alert=True)
        return
    if q.data != "pd:send":
        await q.answer()
    if q.data == "pd:send":
        n = await pd_resend(context)
        await q.answer(f"اتبعتلك {n} معلّق في آخر الشات" if n else "مفيش معلّقات", show_alert=not n)
        return
    await q.edit_message_text(cut_text(await run(pending_report)), parse_mode="HTML", reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 ارسال", callback_data="pd:send")],
        [InlineKeyboardButton("🔄 تحديث", callback_data="pd:open"), InlineKeyboardButton("⬅️ رجوع", callback_data="mn:home")]]))


# ================= بعد تسجيل دفعة من الفورم: عملية أخرى / إغلاق =================
CLOSE_BTN = "✖️ إغلاق"


async def again_menu(context, d):
    uid = d["uid"]
    await ensure_roles()
    url = mini_url({"type": "opay" if uid in ADMINS else "pay", "seller": d.get("seller", ""), "vals": {}})
    rows = [[KeyboardButton("➕ عملية أخرى", web_app=WebAppInfo(url=url))], [KeyboardButton(CLOSE_BTN)]]
    try:
        markup = ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True)
    except TypeError:
        markup = ReplyKeyboardMarkup(rows, resize_keyboard=True)
    await context.bot.send_message(chat_of(uid), "تعمل إيه دلوقتي؟ 👇", reply_markup=markup)


async def restore_kb(uid):
    await ensure_roles()
    if uid in ADMINS:
        return admin_form_kb() if ADMIN_MODE.get(uid) == "f" else menu_kb()
    if cash_uid_ok(uid):
        return cash_kb(mini_url({"type": "cash", "vals": {}}))
    name, role = await run(find_user, uid)
    url = mini_url({"type": "pay", "seller": name or "", "vals": {}})
    if role == "sales_mgr":
        return mh_kb(url)
    return mini_kb(FORM_TITLES["pay"], url)


async def close_btn(update, context):
    kb_ = await restore_kb(update.effective_user.id)
    await update.message.reply_text("تم الإغلاق ✅", reply_markup=kb_)
    raise ApplicationHandlerStop


# ================= تعديل / حذف قيد (المدير والمحاسبين) =================
def ed_find_sync(eno):
    vals = sheet.worksheet(GL_TAB).get_all_values()
    hit = []
    for i, r in enumerate(vals):
        if i == 0:
            continue
        r = list(r) + [""] * (17 - len(r))
        if r[2].strip() == eno:
            hit.append((i + 1, r))
    return hit


def ed_text_of(eno, hit):
    first = hit[0][1]
    out = [f"القيد رقم {eno} ({len(hit)} سطر)",
           f"التاريخ: {first[1]}", f"البيان: {first[3]}", f"المستخدم: {first[0]}", ""]
    for _, r in hit[:12]:
        acct = " > ".join(x for x in r[4:8] if str(x).strip()).replace("_", " ")
        side = f"مدين {num(r[15]):,.2f}" if str(r[15]).strip() else f"دائن {num(r[16]):,.2f}"
        out.append(f"• {acct}: {side}")
    if len(hit) > 12:
        out.append(f"... و{len(hit) - 12} سطر كمان")
    return "\n".join(out)


def ed_delete_sync(eno, rownums, who):
    ws = sheet.worksheet(GL_TAB)
    vals = ws.get_all_values()
    rows = []
    for r in rownums:
        row = list(vals[r - 1]) if r - 1 < len(vals) else []
        if len(row) < 3 or row[2].strip() != eno:
            raise RuntimeError("الشيت اتغيّر من وقت ما بحثت. ابدأ من الأول (/editentry).")
        rows.append(row + [""] * (17 - len(row)))
    try:
        bk = sheet.worksheet("Deleted_Entries")
    except gspread.WorksheetNotFound:
        bk = sheet.add_worksheet("Deleted_Entries", rows=1000, cols=20)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    bk.append_rows([[ts, who] + r for r in rows], value_input_option="RAW")
    for r in sorted(rownums, reverse=True):
        ws.delete_rows(r)
    col = ws.col_values(1)       # ما يفضلش سطرين فاضيين ورا بعض
    kill = [i + 1 for i in range(2, len(col)) if not col[i].strip() and not col[i - 1].strip()]
    for r in sorted(kill, reverse=True):
        ws.delete_rows(r)
    return len(rows)


async def ed_prompt(update, context):
    context.user_data["ed"] = {"wait": True}
    await update.effective_message.reply_text(
        "✏️ اكتب رقم القيد (هو الرقم اللي في عمود الرقم في GL_Entries: رقم الوصل أو رقم الطلب):",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("➕ إنشاء قيد", callback_data="ed:new")],
                                           [InlineKeyboardButton("إلغاء", callback_data="ed:cancel")]]))


async def ed_cmd(update, context):
    await ensure_roles()
    if update.effective_user.id not in ADMINS:
        return
    await ed_prompt(update, context)
    raise ApplicationHandlerStop


async def _ed_cb(update, context):
    q = update.callback_query
    await ensure_roles()
    if q.from_user.id not in ADMINS:
        await q.answer("للمحاسب والمدير بس", show_alert=True)
        return
    st = context.user_data.get("ed") or {}
    act = q.data.split(":")[1]
    if q.data == "menu:edit":
        await q.answer()
        await ed_prompt(update, context)
    elif act == "cancel":
        context.user_data.pop("ed", None)
        await q.answer()
        await q.edit_message_text("اتلغى. مفيش حاجة اتغيّرت.")
    elif act in ("del", "edit"):
        if not st.get("rows"):
            await q.answer("ابدأ من الأول: /editentry", show_alert=True)
            return
        st["mode"] = act
        await q.answer()
        what = ("هحذف القيد ده من GL_Entries (وبحتفظ بنسخة منه في تاب Deleted_Entries)." if act == "del" else
                "هحذف القيد القديم (وبحتفظ بنسخة منه)، وبعدها أفتحلك القايمة تسجّله من جديد صح.")
        await q.edit_message_text(f"{st['text']}\n\n⚠️ {what}\nمتأكد؟", reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ أيوه", callback_data="ed:yes"), InlineKeyboardButton("إلغاء", callback_data="ed:cancel")]]))
    elif act == "yes":
        if not st.get("rows") or not st.get("mode"):
            await q.answer("ابدأ من الأول: /editentry", show_alert=True)
            return
        await q.answer()
        who = ADMIN_USERS.get(q.from_user.id, str(q.from_user.id))
        try:
            n = await run(ed_delete_sync, st["num"], st["rows"], who)
        except Exception as e:
            context.user_data.pop("ed", None)
            await q.edit_message_text(f"❌ ماتمسحش: {e}")
            return
        _GL_CACHE["t"] = 0
        try:
            kick_reports()
        except Exception as e:
            logging.warning("reports refresh failed: %s", e)
        context.user_data.pop("ed", None)
        extra = "\n(لو الشيت التاني GL2 متظبط، القيد لسه فيه ولازم يتمسح منه يدوي.)" if GL2_SHEET_ID else ""
        if st["mode"] == "del":
            await q.edit_message_text(f"🗑 اتمسح القيد رقم {st['num']} ({n} سطر) من GL_Entries.\nنسخة منه محفوظة في تاب Deleted_Entries.{extra}")
        else:
            await q.edit_message_text(f"🗑 اتمسح القيد القديم رقم {st['num']} ({n} سطر). ده اللي كان فيه:\n\n{st['text']}{extra}")
            await q.message.reply_text("اختار نوع العملية وسجّلها من جديد 👇", reply_markup=menu_markup())


async def ed_cb(update, context):
    await _ed_cb(update, context)
    raise ApplicationHandlerStop


async def ed_text(update, context):
    if context.user_data.get("srch_wait") and update.message and update.message.text:
        t0 = update.message.text.strip()
        if t0 in (".", MENU_BTN) or t0.startswith("/"):
            context.user_data.pop("srch_wait", None)
        elif update.effective_user.id in ADMINS:
            await ed_text_search(update, context)
    st = context.user_data.get("ed")
    if not st or not st.get("wait") or not update.message:
        return
    eno = (update.message.text or "").translate(AR_DIGITS).strip()
    if not eno:
        return
    hit = await run(ed_find_sync, eno)
    if not hit:
        await update.message.reply_text("مفيش قيد بالرقم ده في GL_Entries. اكتب رقم تاني، أو اضغط إلغاء.",
                                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("إلغاء", callback_data="ed:cancel")]]))
        raise ApplicationHandlerStop
    text = ed_text_of(eno, hit)
    context.user_data["ed"] = {"wait": False, "num": eno, "rows": [r for r, _ in hit], "text": text}
    await update.message.reply_text(text + "\n\nعايز تعمل إيه؟", reply_markup=InlineKeyboardMarkup([[
        InlineKeyboardButton("✏️ تعديل", callback_data="ed:edit"), InlineKeyboardButton("🗑 حذف", callback_data="ed:del")],
        [InlineKeyboardButton("إلغاء", callback_data="ed:cancel")]]))
    raise ApplicationHandlerStop


async def whoami(update, context):
    """بيقول للمستخدم الدور المكتوب في الشيت وإزاي البوت فهمه (للتشخيص)."""
    uid = update.effective_user.id
    raw, name = "(مش موجود في Users_IDs)", "-"
    try:
        for r in await run(lambda: sheet.worksheet(USERS_TAB).get_all_values()[1:]):
            if len(r) > 1 and re.sub(r"\D", "", r[1].translate(AR_DIGITS)) == str(uid):
                name, raw = r[0].strip(), (r[2].strip() if len(r) > 2 else "") or "(فاضي)"
                break
    except Exception as e:
        raw = f"خطأ في قراءة الشيت: {e}"
    await update.message.reply_text(
        f"ID: {uid}\\nالاسم في الشيت: {name}\\nالدور المكتوب: {raw}\\nالبوت فاهمه: {role_key(raw) or 'مش معروف'}\\n"
        f"الفورم متفعّلة: {'أيوه' if MINIAPP_URL else 'لأ (MINIAPP_URL فاضي)'}\\n\\n"
        "مسؤول المبيعات لازم يتكتب دوره: مسؤول مبيعات")


# ================= إذاعة رسالة لمجموعات (للمدير بس: /broadcast) =================
BC_GROUPS = [("seller", "البياعين"), ("accountant", "المحاسبين"), ("ceo", "CEO"),
             ("sm", "SM"), ("cash", "أمناء الخزنة"), ("operation", "Operation")]
BC_ALL = {k for k, _ in BC_GROUPS}


def bc_members(keys):
    """{telegram_id: اسم} للمجموعات المختارة. لو اتختارت كل المجموعات، بيدخل كل اللي في Users_IDs."""
    users = read_users()
    names = {i: n for i, n, _ in users}
    out = {}

    def add(uid, fallback=""):
        if uid and uid != OWNER_ID:
            out[uid] = names.get(uid) or fallback or str(uid)

    everyone = keys >= BC_ALL
    for uid, _, role in users:
        if everyone:
            add(uid)
        if "seller" in keys and role in ("seller", "sales_mgr", "install_mgr"):
            add(uid)
        if "accountant" in keys and role == "accountant":
            add(uid)
        if "ceo" in keys and role == "ceo":
            add(uid)
        if "sm" in keys and role == "sm":
            add(uid)
        if "cash" in keys and role in ("cash_main", "cash_sub"):
            add(uid)
        if "operation" in keys and role == "operation":
            add(uid)
    if "accountant" in keys:
        for uid in ADMINS:
            add(uid, ADMIN_USERS.get(uid, ""))
    if "ceo" in keys:
        for uid in CEO_IDS:
            add(uid, CEO_NAME)
    if "sm" in keys and GOMAA_ID:
        add(GOMAA_ID, "م/ محمد جمعة")
    if "cash" in keys:
        for uid in set(CASH_MAIN_IDS) | set(CASH_SUB_IDS):
            add(uid)
    if "operation" in keys:
        for uid in OPERATION_IDS:
            add(uid)
    return out


def bc_kb(sel):
    rows = [[InlineKeyboardButton(("✅ " if sel >= BC_ALL else "⬜ ") + "كل المستخدمين", callback_data="bc:all")]]
    btns = [InlineKeyboardButton(("✅ " if k in sel else "⬜ ") + lbl, callback_data=f"bc:t:{k}") for k, lbl in BC_GROUPS]
    rows += [btns[i:i + 2] for i in range(0, len(btns), 2)]
    rows.append([InlineKeyboardButton("▶️ التالي", callback_data="bc:next"),
                 InlineKeyboardButton("إلغاء", callback_data="bc:cancel")])
    return InlineKeyboardMarkup(rows)


def bc_reset(context):
    for k in ("bc_sel", "bc_wait", "bc_text"):
        context.user_data.pop(k, None)


async def bc_cmd(update, context):
    if update.effective_user.id != OWNER_ID:
        return
    bc_reset(context)
    context.user_data["bc_sel"] = set()
    await update.message.reply_text("📢 اختار مين يستلم الرسالة (تقدر تختار أكتر من واحد):", reply_markup=bc_kb(set()))
    raise ApplicationHandlerStop


async def _bc_send(bot, members, text):
    """الإرسال بيشتغل في الخلفية عشان ما يعطلش باقي المستخدمين."""
    ok, bad = 0, []
    for uid, nm in members.items():
        try:
            await bot.send_message(uid, text)
            ok += 1
        except Exception as e:
            logging.warning("broadcast to %s failed: %s", uid, e)
            bad.append(nm)
        await asyncio.sleep(0.05)
    msg = f"✅ الرسالة وصلت لـ {ok} واحد."
    if bad:
        msg += f"\n⚠️ ماوصلتش لـ {len(bad)}: " + "، ".join(bad[:30]) + "\n(غالبًا لسه ماداسوش /start على البوت)"
    try:
        await bot.send_message(OWNER_ID, msg)
    except Exception as e:
        logging.warning("broadcast report failed: %s", e)


async def _bc_cb(update, context):
    q = update.callback_query
    if q.from_user.id != OWNER_ID:
        await q.answer("للمدير بس", show_alert=True)
        return
    parts = q.data.split(":")
    act = parts[1] if len(parts) > 1 else ""
    sel = context.user_data.setdefault("bc_sel", set())
    if act == "t" and len(parts) > 2 and parts[2] in BC_ALL:
        sel ^= {parts[2]}
    elif act == "all":
        if sel >= BC_ALL:
            sel.clear()
        else:
            sel.update(BC_ALL)
    elif act == "cancel":
        bc_reset(context)
        await q.answer()
        await q.edit_message_text("اتلغت الإذاعة. مفيش حاجة اتبعتت.")
        return
    elif act == "next":
        if not sel:
            await q.answer("اختار مجموعة واحدة على الأقل", show_alert=True)
            return
        context.user_data["bc_wait"] = True
        await q.answer()
        await q.edit_message_text("✍️ اكتب الرسالة اللي عايز تبعتها دلوقتي (رسالة واحدة):",
                                  reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("إلغاء", callback_data="bc:cancel")]]))
        return
    elif act == "send":
        text = context.user_data.get("bc_text")
        if not text or not sel:
            await q.answer("مفيش رسالة جاهزة. ابدأ من /broadcast", show_alert=True)
            return
        await q.answer()
        await ensure_roles()
        members = await run(bc_members, set(sel))
        bc_reset(context)
        await q.edit_message_text(f"⏳ بدأت الإرسال لـ {len(members)} واحد. هيجيلك تقرير لما يخلص، وتقدر تكمل شغلك عادي.")
        context.application.create_task(_bc_send(context.bot, members, text))
        return
    await q.answer()
    await q.edit_message_reply_markup(reply_markup=bc_kb(sel))


async def bc_cb(update, context):
    await _bc_cb(update, context)
    raise ApplicationHandlerStop


async def bc_got_text(update, context, txt):
    """بيتنادى من owner_text_hook لما المدير يكتب نص الإذاعة."""
    context.user_data.pop("bc_wait", None)
    context.user_data["bc_text"] = txt
    sel = set(context.user_data.get("bc_sel", set()))
    await ensure_roles()
    members = await run(bc_members, sel)
    labels = "كل المستخدمين" if sel >= BC_ALL else "، ".join(l for k, l in BC_GROUPS if k in sel)
    if not members:
        bc_reset(context)
        await update.message.reply_text(f"مفيش حد مسجّل في ({labels}) أبعتله. راجع تاب Users_IDs.")
        return
    await update.message.reply_text(
        f"📢 هتتبعت لـ {len(members)} واحد ({labels}):\n\n{txt}\n\nتبعت؟",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ ابعت", callback_data="bc:send"),
                                            InlineKeyboardButton("❌ إلغاء", callback_data="bc:cancel")]]))



# ================= شجرة الحسابات: فورم إنشاء قيد (فورم أو لصق من اليومية) =================
DENOMS = [200, 100, 50, 20, 10, 5, 1]
CASH_L2 = "النقدية_بالخزينة"
CUS_EMP_L3 = {nk(x) for x in ("العهد_المؤقتة", "العهد_المستديمة", "سلف_العاملين")}


def gen_kids(form):
    """أبناء العقدة الحالية في متصفح الشجرة (مع فلتر الكتابة والصفحات)."""
    path = form.get("tpath") or []
    if len(path) == 3 and nk(path[2]) in CUS_EMP_L3:
        kids = [e.strip() for e in EMPLOYEES]
    else:
        kids = tree_kids(path)
    f = norm(form.get("tfilter") or "")
    if f:
        kids = [k for k in kids if f in norm(k)]
    return kids


def tree_view(form):
    key = form["cur"]
    label = {"dr": "المدين", "cr": "الدائن"}.get(key, key)
    path = form.get("tpath") or []
    kids = gen_kids(form)
    page = form.get("tpage", 0)
    size = 20
    shown = kids[page * size:(page + 1) * size]
    btns = [InlineKeyboardButton(k.replace("_", " "), callback_data=f"f:tk:{page * size + i}") for i, k in enumerate(shown)]
    rows = pairs(btns)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ السابق", callback_data=f"f:tp:{page - 1}"))
    if (page + 1) * size < len(kids):
        nav.append(InlineKeyboardButton("التالي ▶️", callback_data=f"f:tp:{page + 1}"))
    if nav:
        rows.append(nav)
    ctl = []
    if path and (len(path) >= 2 or not tree_kids(path)):
        ctl.append(InlineKeyboardButton("✅ اعتماد المسار ده", callback_data="f:tok"))
    if len(path) >= 2 and len(path) < 4:
        ctl.append(InlineKeyboardButton("✍️ اكتب اسم جديد", callback_data="f:tw"))
    if ctl:
        rows.append(ctl)
    rows.append(([InlineKeyboardButton("⬆️ مستوى أعلى", callback_data="f:tu")] if path else []) +
                [InlineKeyboardButton("⬅️ رجوع للفورم", callback_data="f:b")])
    text = (f"🌳 اختار {label}\nالمسار: {' > '.join(p.replace('_', ' ') for p in path) or '—'}\n"
            + (f"(فلتر: {form['tfilter']})\n" if form.get("tfilter") else "")
            + ("\n" + ("اختار المستوى الجاي، أو اكتب جزء من الاسم عشان تدوّر." if kids else
                        "مفيش مستويات بعد كده. اضغط اعتماد المسار، أو اكتب اسم جديد." if path else "مفيش بيانات."))
            + ("\nعدد الاختيارات: " + str(len(kids)) if kids else ""))
    return text, InlineKeyboardMarkup(rows)


async def gen_cb(update, context, form, parts):
    q = update.callback_query
    act = parts[1]
    v = form["vals"]
    if act == "o":
        key = parts[2]
        form.update(mode="tree", cur=key, tpath=list(v.get(key) or []), tfilter="", tpage=0, tnew=False)
    elif act == "tk":
        kids = gen_kids(form)
        i = int(parts[2])
        if i < len(kids):
            form["tpath"] = (form.get("tpath") or []) + [kids[i]]
            form.update(tfilter="", tpage=0, tnew=False)
    elif act == "tu":
        if form.get("tpath"):
            form["tpath"].pop()
        form.update(tfilter="", tpage=0, tnew=False)
    elif act == "tp":
        form["tpage"] = int(parts[2])
    elif act == "tw":
        form["tnew"] = True
        await q.answer("اكتب الاسم الجديد في رسالة", show_alert=False)
        return F_VIEW
    elif act == "tok":
        v[form["cur"]] = list(form.get("tpath") or [])
        form.update(mode="edit", tfilter="", tpage=0)
    elif act == "paste":
        form["mode"] = "paste"
    elif act == "pgo":
        await q.answer()
        return await gen_post_paste(update, context, form)
    await q.answer()
    await form_refresh(context, form)
    return F_VIEW


def _paste_date(x):
    x = str(x).strip().split(" ")[0]
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%d-%b-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(x, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return None


def parse_gl_paste(raw, user):
    """بيحوّل قيود منسوخة من اليومية (Excel) لصفوف GL_Entries (17 عمود). بيرجّع (صفوف, أخطاء)."""
    rows, errs = [], []
    for n, line in enumerate([l for l in raw.replace("\r", "").split("\n") if l.strip()], 1):
        cells = [c.strip() for c in (line.split("\t") if "\t" in line else (line.split("|") if "|" in line else line.split(";")))]
        k = next((i for i in range(min(3, len(cells))) if _paste_date(cells[i])), None)
        if k is None:
            errs.append(f"سطر {n}: مش لاقي التاريخ")
            continue
        body = cells[k:k + 16]
        body += [""] * (16 - len(body))
        who = cells[k - 1] if k >= 1 and cells[k - 1] else user
        row = [who, _paste_date(body[0])] + body[1:16]
        row[2] = str(row[2]).translate(AR_DIGITS)
        for j in (15, 16):
            row[j] = round(num(str(row[j]).translate(AR_DIGITS)), 2) if str(row[j]).strip() else ""
        if row[15] == "" and row[16] == "":
            errs.append(f"سطر {n}: مفيش مدين ولا دائن")
            continue
        if row[8] == "":
            row[8] = 0
        rows.append(row)
    if rows and not errs:
        dr = sum(num(r[15]) for r in rows)
        cr = sum(num(r[16]) for r in rows)
        if abs(dr - cr) > 0.01:
            errs.append(f"القيد مش متزن: مدين {dr:,.2f} ودائن {cr:,.2f}")
    return rows, errs


def paste_preview(rows):
    out = [f"📋 هتتسجل {len(rows)} سطر:", ""]
    for r in rows[:25]:
        acct = " > ".join(str(x) for x in r[4:8] if str(x).strip()).replace("_", " ")
        side = f"مدين {num(r[15]):,.2f}" if str(r[15]).strip() else f"دائن {num(r[16]):,.2f}"
        out.append(f"• {r[1]} | {r[2] or '-'} | {acct}: {side}")
    out.append(f"\nالبيان: {rows[0][3]}")
    bad = [r[4] for r in rows if r[4] and r[4] not in TREE_L1]
    if bad:
        out.append("⚠️ مستوى1 مش في الشجرة: " + "، ".join(dict.fromkeys(bad)))
    return "\n".join(out)


async def gen_text(update, context, form, raw):
    mode = form.get("mode")
    uid = form["uid"]
    if mode == "paste":
        user = ADMIN_USERS.get(uid, ACCOUNTANTS[0])
        rows, errs = await run(parse_gl_paste, raw, user)
        if errs:
            await context.bot.send_message(form["chat"], "⚠️ " + "\n".join(errs[:8]) + "\nابعتها تاني بعد التصليح.")
            return F_VIEW
        form["paste"] = rows
        form.update(mode="result", result=paste_preview(rows),
                    result_kb=[[InlineKeyboardButton("✅ تسجيل", callback_data="f:pgo"), InlineKeyboardButton("⬅️ رجوع", callback_data="f:b")]])
        await form_refresh(context, form)
        return F_VIEW
    if mode == "tree":
        path = form.get("tpath") or []
        if form.get("tnew") and 1 <= len(path) < 4:
            form["tpath"] = path + [raw.strip()[:100]]
            form.update(tnew=False, tfilter="", tpage=0)
        else:
            form["tfilter"] = raw.strip()
            form["tpage"] = 0
            if not gen_kids(form) and 2 <= len(path) < 4:       # مفيش تطابق: نعتبره اسم جديد
                form["tpath"] = path + [raw.strip()[:100]]
                form["tfilter"] = ""
        await form_refresh(context, form)
        return F_VIEW
    if form["type"] == "tsearch":
        form["vals"]["voucher"] = raw.strip()
        text = await run(tz_search_text, raw.strip(), uid)
        form.update(mode="result", result=text, result_kb=[[InlineKeyboardButton("🔎 بحث تاني", callback_data="f:b"),
                                                         InlineKeyboardButton("إغلاق", callback_data="f:c")]])
        form["vals"].pop("voucher", None)
        await form_refresh(context, form)
        return F_VIEW
    if form["type"] == "tcount":
        v = form["vals"]
        for line in [l.strip() for l in raw.replace("：", ":").splitlines() if l.strip()]:
            line = line.translate(AR_DIGITS)
            m = re.match(r"^(?:فئة\s*)?(\d+)\s*[:=\-]\s*(\d+)$", line)
            if m and int(m.group(1)) in DENOMS:
                v[f"k_{m.group(1)}"] = int(m.group(2))
            elif line.startswith(("فكة", "فكه", "معدن")) and ":" in line:
                try:
                    v["k_coin"] = float(line.split(":", 1)[1].replace(",", "").strip())
                except ValueError:
                    pass
            elif line.startswith("ملاح") and ":" in line:
                v["tnote"] = line.split(":", 1)[1].strip()
        await form_refresh(context, form)
        return F_VIEW
    return None


def gen_lines(v, user, desc_extra=""):
    eno = v.get("voucher") or ("M" + datetime.now().strftime("%d%H%M%S"))
    a = lambda p: (list(p) + ["", "", "", ""])[:4]
    amt = v["amount"]
    common = [0, "", v.get("age", ""), v.get("cc1", ""), v.get("cc2", ""), v.get("client", ""), v.get("project", "")]
    head = [user, v["date"], eno, v["bayan"]]
    return eno, [head + a(v["dr"]) + common + [amt, ""], head + a(v["cr"]) + common + ["", amt]]


async def gen_after_post(context, lines, user, att, date, desc):
    """سجل الخزنة + المرفق بعد تسجيل القيد."""
    for ln in lines:
        if nk(ln[5]) == nk(CASH_L2):
            incoming = str(ln[15]).strip() not in ("", "0")
            try:
                await run(cash_row, ln[6], ln[1], ln[2], None if incoming else ln[16], ln[15] if incoming else None, "قيد يدوي", user)
            except Exception as e:
                logging.warning("cash tab row failed: %s", e)
    if att:
        return await save_ref(context, "قيود يدوية", user, att_ref(att), desc, date)
    return ""


async def gen_send(update, context, form):
    v = form["vals"]
    uid = form["uid"]
    user = ADMIN_USERS.get(uid, ACCOUNTANTS[0])
    eno, lines = gen_lines(v, user)
    try:
        await run(gl_append, lines)
    except Exception as e:
        await update.effective_message.reply_text(f"❌ القيد ماتسجلش: {e}")
        return F_VIEW
    path = await gen_after_post(context, lines, user, v.get("att"), v["date"], v["bayan"])
    context.user_data.pop("form", None)
    await say(update, f"✅ اتسجّل القيد رقم {eno}\n\nمدين: {form_val(form, 'dr')}\nدائن: {form_val(form, 'cr')}\n"
                      f"المبلغ: {v['amount']:,.2f}\nالتاريخ: {v['date']}\nالبيان: {v['bayan']}"
              + ("\nالمرفق اتحفظ ✅" if path else ""))
    return ConversationHandler.END


async def gen_post_paste(update, context, form):
    rows = form.get("paste") or []
    uid = form["uid"]
    user = ADMIN_USERS.get(uid, ACCOUNTANTS[0])
    if not rows:
        return F_VIEW
    if not any(str(r[2]).strip() for r in rows):
        eno = "M" + datetime.now().strftime("%d%H%M%S")
        for r in rows:
            r[2] = eno
    rows = [[user if not str(r[0]).strip() else r[0]] + r[1:] for r in rows]
    try:
        await run(gl_append, rows)
    except Exception as e:
        await update.effective_message.reply_text(f"❌ القيد ماتسجلش: {e}")
        return F_VIEW
    await gen_after_post(context, rows, user, None, rows[0][1], rows[0][3])
    context.user_data.pop("form", None)
    await say(update, f"✅ اتسجّل {len(rows)} سطر في GL_Entries (رقم القيد: {rows[0][2]}).")
    return ConversationHandler.END


async def gen_start(update, context):
    await ensure_roles()
    if update.callback_query:
        await update.callback_query.answer()
    if update.effective_user.id not in ADMINS:
        return ConversationHandler.END
    return await owner_form_open(update, context, "gen")


# ================= الخزنة: جرد + تقارير + بحث برقم الوصل (أمين الخزنة والمدير) =================
TZ_SEARCH_BTN = "🔎 بحث برقم الوصل"
TZ_INV_BTN = "📦 جرد الخزنة والتقارير"
CASH_COUNT_TAB = "Cash_Counts"


def cash_kb(url):
    rows = [[KeyboardButton("📝 فورم حركة الخزنة", web_app=WebAppInfo(url=url))], [KeyboardButton(TZ_SEARCH_BTN)], [KeyboardButton(TZ_INV_BTN)]]
    try:
        return ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True)
    except TypeError:
        return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def cash_menu_markup():
    return InlineKeyboardMarkup([[InlineKeyboardButton("📝 تسجيل حركة (فورم)", callback_data="cm:form")],
                                 [InlineKeyboardButton(TZ_SEARCH_BTN, callback_data="tz:search")],
                                 [InlineKeyboardButton(TZ_INV_BTN, callback_data="ti:home")]])


async def cash_home(update, context):
    """أمين الخزنة: التلات حاجات (تسجيل + بحث + جرد وتقارير)."""
    context.user_data.clear()
    if MINIAPP_URL:
        await update.effective_message.reply_text("اختار من الأزرار اللي تحت 👇", reply_markup=cash_kb(mini_url({"type": "cash", "vals": {}})))
    else:
        await update.effective_message.reply_text("تفتح إيه؟", reply_markup=cash_menu_markup())
    return ConversationHandler.END


def tz_scope(uid):
    """(اسم الخزنة, حساب L3 أو None لكل الخزن)."""
    if uid in ADMINS and not cash_uid_ok(uid):
        return "كل الخزن", None
    sub = uid in CASH_SUB_IDS and uid not in CASH_MAIN_IDS
    try:
        l3 = cash_account(read_lists(), sub)
    except Exception:
        l3 = None
    return ("الخزنة الفرعية" if sub else "الخزنة الرئيسية"), l3


def tz_rows(l3):
    _GL_CACHE["t"] = 0
    return [r for r in read_gl() if nk(r[5]) == nk(CASH_L2) and (not l3 or r[6].strip() == l3)]


def tz_ok(uid):
    return uid in ADMINS or cash_uid_ok(uid)


def _mv(r):
    dr, cr = num(r[15]), num(r[16])
    return f"▫️ {r[1]} | وصل {r[2] or '-'} | {str(r[3])[:38]} | " + (f"+{money(dr)}" if dr else f"-{money(cr)}")


def tz_home_text(uid):
    label, l3 = tz_scope(uid)
    rows = tz_rows(l3)
    bal = {}
    for r in rows:
        bal[r[6].strip()] = bal.get(r[6].strip(), 0) + num(r[15]) - num(r[16])
    today = _cairo_now().date()
    now = _cairo_now()
    d_in = d_out = m_in = m_out = 0.0
    for r in rows:
        dt = parse_date(r[1])
        if not dt:
            continue
        dr, cr = num(r[15]), num(r[16])
        if dt.date() == today:
            d_in += dr
            d_out += cr
        if dt.year == now.year and dt.month == now.month:
            m_in += dr
            m_out += cr
    lines = [f"📦 جرد {label}", "━━━━━━━━━━━━━━━", f"💰 آخر رصيد: {money(sum(bal.values()))} ج"]
    if len(bal) > 1 or l3 is None:
        lines += [f"   ▫️ {k.replace('_', ' ')}: {money(v)}" for k, v in sorted(bal.items()) if abs(v) >= 0.5]
    last = rows[-1] if rows else None
    lines.append("🕑 آخر حركة: " + (f"{last[1]} | وصل {last[2] or '-'} | {str(last[3])[:40]}" if last else "مفيش"))
    lines += ["", f"📅 النهاردة: داخل {money(d_in)} | خارج {money(d_out)} | صافي {money(d_in - d_out)}",
              f"🗓 الشهر: داخل {money(m_in)} | خارج {money(m_out)} | صافي {money(m_in - m_out)}"]
    return "\n".join(lines)


def tz_home_markup():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📅 حركة النهاردة", callback_data="ti:day"), InlineKeyboardButton("🗓 حركة الشهر", callback_data="ti:month")],
        [InlineKeyboardButton("🕑 آخر 10 حركات", callback_data="ti:last"), InlineKeyboardButton("📊 حسب النوع", callback_data="ti:kinds")],
        [InlineKeyboardButton("⏳ طلباتي وحالتها", callback_data="ti:pend"), InlineKeyboardButton("🧮 جرد فعلي (عدّ الفلوس)", callback_data="ti:count")],
        [InlineKeyboardButton("🔄 تحديث", callback_data="ti:home"), InlineKeyboardButton("✖️ إغلاق", callback_data="ti:close")]])


def tz_report(uid, kind):
    label, l3 = tz_scope(uid)
    rows = tz_rows(l3)
    now = _cairo_now()
    if kind in ("day", "month"):
        sel = []
        for r in rows:
            dt = parse_date(r[1])
            if dt and ((kind == "day" and dt.date() == now.date()) or (kind == "month" and dt.year == now.year and dt.month == now.month)):
                sel.append(r)
        tin, tout = sum(num(r[15]) for r in sel), sum(num(r[16]) for r in sel)
        head = f"{'📅 حركة النهاردة' if kind == 'day' else '🗓 حركة الشهر'} - {label}\n━━━━━━━━━━━━━━━\n"
        body = "\n".join(_mv(r) for r in sel[-40:]) or "▫️ مفيش حركات"
        return head + f"عدد الحركات: {len(sel)}\nداخل: {money(tin)} ج | خارج: {money(tout)} ج | صافي: {money(tin - tout)} ج\n\n" + body
    if kind == "last":
        return f"🕑 آخر 10 حركات - {label}\n━━━━━━━━━━━━━━━\n" + ("\n".join(_mv(r) for r in rows[-10:][::-1]) or "▫️ مفيش")
    if kind == "kinds":
        agg = {}
        for name in (["الخزنة الرئيسية", "الخزنة الفرعية"] if l3 is None else [cash_sheet_name(l3)]):
            try:
                ws = sheet.worksheet(name)
            except gspread.WorksheetNotFound:
                continue
            for r in ws.get_all_values()[2:]:
                r = r + [""] * (9 - len(r))
                dt = parse_date(r[0])
                if dt and dt.year == now.year and dt.month == now.month:
                    k = r[6].strip() or "غير محدد"
                    a = agg.setdefault(k, [0.0, 0.0])
                    a[0] += num(r[4])
                    a[1] += num(r[3])
        body = "\n".join(f"▫️ {k}: داخل {money(a[0])} | خارج {money(a[1])}" for k, a in sorted(agg.items())) or "▫️ مفيش"
        return f"📊 حركة الشهر حسب النوع - {label}\n━━━━━━━━━━━━━━━\n" + body
    if kind == "pend":
        try:
            vals = sheet.worksheet(CASH_PEND_TAB).get_all_values()[1:]
        except gspread.WorksheetNotFound:
            vals = []
        mine = []
        for r in vals:
            r = r + [""] * (14 - len(r))
            if uid in ADMINS and not cash_uid_ok(uid) or re.sub(r"\D", "", r[13]) == str(uid):
                mine.append(r)
        icon = {"مستني اعتماد": "⏳", "مرفوض": "❌"}
        body = "\n".join(f"{icon.get(r[10].strip(), '✅')} {r[2]} | وصل {r[3]} | {r[6]} {num(r[5]):,.0f} | {r[10]}" for r in mine[-15:][::-1]) or "▫️ مفيش طلبات"
        return f"⏳ طلبات الخزنة وحالتها\n━━━━━━━━━━━━━━━\n" + body
    return ""


def tz_search_text(voucher, uid):
    label, l3 = tz_scope(uid)
    key = voucher.translate(AR_DIGITS).strip()
    same = lambda x: str(x).translate(AR_DIGITS).strip() == key or (key.isdigit() and str(x).strip().isdigit() and int(str(x)) == int(key))
    _GL_CACHE["t"] = 0
    gl = [r for r in read_gl() if same(r[2])]
    mine = any(nk(r[5]) == nk(CASH_L2) and (not l3 or r[6].strip() == l3) for r in gl)
    out = [f"🔎 نتيجة البحث عن وصل {key}", "━━━━━━━━━━━━━━━"]
    if gl and mine:
        out += ["✅ اتسجّل في القيود:", f"التاريخ: {gl[0][1]}", f"البيان: {gl[0][3]}", f"المحاسب: {gl[0][0]}"]
        for r in gl[:10]:
            acct = " > ".join(x for x in r[4:8] if str(x).strip()).replace("_", " ")
            out.append(f"• {acct}: " + (f"مدين {num(r[15]):,.2f}" if str(r[15]).strip() else f"دائن {num(r[16]):,.2f}"))
    try:
        for r in sheet.worksheet(CASH_PEND_TAB).get_all_values()[1:]:
            r = r + [""] * (14 - len(r))
            if same(r[3]) and (uid in ADMINS and not cash_uid_ok(uid) or re.sub(r"\D", "", r[13]) == str(uid) or l3):
                out += ["", f"📋 طلب خزنة ({r[10]}):", f"{r[6]} {num(r[5]):,.2f} | {r[7]}", f"البيان: {r[4]}", f"التاريخ: {r[2]} | أمين الخزنة: {r[9]}"]
    except gspread.WorksheetNotFound:
        pass
    try:
        for r in sheet.worksheet("Pending").get_all_values()[1:]:
            r = r + [""] * (17 - len(r))
            if same(r[15]) and r[11].strip() == "معلّق":
                out += ["", "⏳ تحصيل بياع مستني اعتماد:", f"{r[6]} | {r[5]} | {num(r[4]):,.2f} | {r[2]}"]
    except Exception:
        pass
    if len(out) == 2:
        out.append("مفيش عملية بالرقم ده.")
    return "\n".join(out)[:3900]


async def tz_inv_open(update, context):
    await ensure_roles()
    uid = update.effective_user.id
    if not tz_ok(uid):
        return ConversationHandler.END
    if update.callback_query:
        await update.callback_query.answer()
    text = await run(tz_home_text, uid)
    await update.effective_message.reply_text(text, reply_markup=tz_home_markup())
    if update.message and update.message.text == TZ_INV_BTN:
        raise ApplicationHandlerStop
    return ConversationHandler.END


async def ti_cb(update, context):
    await ensure_roles()
    q = update.callback_query
    uid = q.from_user.id
    if not tz_ok(uid):
        await q.answer("غير مسموح", show_alert=True)
        return
    kind = q.data.split(":")[1]
    if kind == "close":
        await q.answer()
        try:
            await q.message.delete()
        except Exception:
            pass
        return
    await q.answer()
    if kind in ("home", "ref"):
        text, mk = await run(tz_home_text, uid), tz_home_markup()
    else:
        text = await run(tz_report, uid, kind)
        mk = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ رجوع للجرد", callback_data="ti:home")]])
    try:
        await q.edit_message_text(text[:3900], reply_markup=mk)
    except Exception as e:
        logging.info("ti edit skipped: %s", e)


async def _tz_form(update, context, typ):
    await ensure_roles()
    uid = update.effective_user.id
    if not tz_ok(uid):
        return ConversationHandler.END
    if update.callback_query:
        await update.callback_query.answer()
    context.user_data.clear()
    admin = uid in ADMINS and not cash_uid_ok(uid)
    form = {"type": typ, "uid": uid, "name": ADMIN_USERS.get(uid) or (await run(find_user, uid))[0] or update.effective_user.full_name,
            "opts": [], "L": {}, "vals": {}, "admin": admin, "native": True}
    return await form_open(update, context, form)


async def tz_search_start(update, context):
    return await _tz_form(update, context, "tsearch")


async def tcount_start(update, context):
    return await _tz_form(update, context, "tcount")


async def tcount_send(update, context, form):
    v = form["vals"]
    uid = form["uid"]
    counts = {d: int(v.get(f"k_{d}") or 0) for d in DENOMS}
    coin = num(v.get("k_coin") or 0)
    total = sum(d * c for d, c in counts.items()) + coin
    if total <= 0:
        if update.callback_query:
            await update.callback_query.answer("اكتب عدد الورق من كل فئة الأول", show_alert=True)
        return F_VIEW
    if form.get("admin"):
        sub = v.get("tsub") == "الخزنة الفرعية"
        label = v.get("tsub")
        try:
            l3 = cash_account(read_lists(), sub)
        except Exception:
            l3 = None
    else:
        label, l3 = tz_scope(uid)
    book = sum(num(r[15]) - num(r[16]) for r in tz_rows(l3))
    diff = total - book
    state = "مطابق ✅" if abs(diff) < 0.5 else (f"زيادة {money(diff)} ج ⚠️" if diff > 0 else f"عجز {money(-diff)} ج ⚠️")
    det = " | ".join(f"{d}×{c}" for d, c in counts.items() if c) + (f" | فكة {money(coin)}" if coin else "")
    ws, new = _tab(CASH_COUNT_TAB, 1000, 9)
    if new:
        ws.update("A1", [["وقت الجرد", "التاريخ", "الخزنة", "أمين الخزنة", "الرصيد بالدفاتر", "المعدود فعليًا", "الفرق", "التفاصيل", "ملاحظات"]])
    await run(_append, ws, [datetime.now().strftime("%Y-%m-%d %H:%M:%S"), _today_str(), label or "", form["name"], round(book, 2), round(total, 2),
                            round(diff, 2), det, v.get("tnote", "")])
    msg = (f"🧮 نتيجة جرد {label}\n━━━━━━━━━━━━━━━\nالرصيد بالدفاتر: {money(book)} ج\nالمعدود فعليًا: {money(total)} ج\n"
           f"الفرق: {state}\nالتفاصيل: {det}\n\nاتسجّل في تاب {CASH_COUNT_TAB}. مفيش قيد اتسجل.")
    context.user_data.pop("form", None)
    await say(update, msg)
    if uid != OWNER_ID and abs(diff) >= 0.5:
        try:
            await context.bot.send_message(OWNER_ID, f"⚠️ جرد {label} من {form['name']}:\n" + msg.split("\n", 2)[2])
        except Exception as e:
            logging.warning("count notify failed: %s", e)
    return ConversationHandler.END


async def cash_owner_menu(update, context):
    await ensure_roles()
    q = update.callback_query
    if update.effective_user.id not in ADMINS:
        if q:
            await q.answer("للمحاسبين بس", show_alert=True)
        return
    if q:
        await q.answer()
    mk = InlineKeyboardMarkup(list(cash_menu_markup().inline_keyboard) + [[InlineKeyboardButton("⬅️ رجوع", callback_data="gh:menu")]])
    if q:
        await say(update, "🏦 الخزنة", mk)
    else:
        await update.message.reply_text("🏦 الخزنة", reply_markup=mk)


async def go_home_cb(update, context):
    q = update.callback_query
    if q.from_user.id not in ADMINS:
        await q.answer()
        return
    await say(update, "تفتح إيه؟", menu_markup())


# ================= تابات العهدة: م/ جمعه وم/ محمد حسن (بتتحدث مع كل قيد) =================
CUS_TAB_GOMAA = "تسوية عهدة م جمعه"
CUS_TAB_HASSAN = "تسوية عهدة م محمد حسن"
CUS_TAB_HEADER = ["التاريخ", "رقم", "البيان", "مستلم (مدين)", "منصرف (دائن)", "الرصيد", "المسجّل", "وقت التسجيل"]


def _custody_people():
    return ((CUSTODY_PERSON, CUS_TAB_GOMAA), (SETTLE_EMP, CUS_TAB_HASSAN))


def _is_cus_line(ln, person):
    return (str(ln[5]).strip() == CUSTODY_L2 and str(ln[6]).strip() in CUSTODY_TYPES and norm(str(ln[7])) == norm(person))


def custody_tabs_log(lines):
    for person, tab in _custody_people():
        mine = [ln for ln in lines if _is_cus_line(ln, person)]
        if not mine:
            continue
        ws, new = _tab(tab, 2000, 8)
        if new:
            ws.update("A1", [CUS_TAB_HEADER])
        for ln in mine:
            res = ws.append_row([ln[1], ln[2], ln[3], ln[15], ln[16], "", ln[0], datetime.now().strftime("%Y-%m-%d %H:%M:%S")],
                                value_input_option="USER_ENTERED", table_range="A1")
            row = int(re.search(r"(\d+)$", res["updates"]["updatedRange"]).group(1))
            ws.update_acell(f"F{row}", f"=SUM($D$2:D{row})-SUM($E$2:E{row})")


def custody_tabs_rebuild():
    _GL_CACHE["t"] = 0
    rows = read_gl()
    n = 0
    for person, tab in _custody_people():
        ws, _ = _tab(tab, 2000, 8)
        mine = [r for r in rows if _is_cus_line(r, person)]
        data = [CUS_TAB_HEADER] + [[r[1], r[2], r[3], r[15], r[16], f"=SUM($D$2:D{i})-SUM($E$2:E{i})", r[0], ""] for i, r in enumerate(mine, 2)]
        ws.clear()
        ws.update("A1", data, value_input_option="USER_ENTERED")
        n += len(mine)
    return n


async def custodytabs_cmd(update, context):
    if update.effective_user.id != OWNER_ID:
        return
    n = await run(custody_tabs_rebuild)
    await update.message.reply_text(f"✅ اتبنى تابين العهدة ({CUS_TAB_GOMAA} و {CUS_TAB_HASSAN}) من القيود، فيهم {n} حركة. وبعد كده بيتحدثوا لوحدهم مع كل قيد.")


# ================= بحث في أي حاجة =================
def gl_search(qtext):
    toks = [norm(t) for t in qtext.translate(AR_DIGITS).split() if t.strip()]
    if not toks:
        return "اكتب كلمة للبحث."
    _GL_CACHE["t"] = 0
    hits = []
    for r in read_gl():
        blob = norm(" ".join(str(x) for x in (r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[12], r[13], r[14], r[15], r[16])))
        if all(t in blob for t in toks):
            hits.append(r)
    out = [f"🔎 بحث: {qtext}", "━━━━━━━━━━━━━━━", f"في القيود: {len(hits)} سطر"]
    for r in hits[-15:][::-1]:
        acct = " > ".join(x for x in r[4:8] if str(x).strip()).replace("_", " ")
        out.append(f"• {r[1]} | رقم {r[2] or '-'} | {str(r[3])[:40]} | {acct} | " + (f"مدين {num(r[15]):,.0f}" if str(r[15]).strip() else f"دائن {num(r[16]):,.0f}"))
    for tab, cols, title in (("Pending", (0, 2, 3, 4, 5, 6, 11), "طلبات التحصيل"), (TQ_TAB, (0, 2, 5, 7, 9), "التحويلات"),
                             (CASH_PEND_TAB, (0, 2, 3, 4, 5, 9, 10), "طلبات الخزنة")):
        try:
            found = [r for r in sheet.worksheet(tab).get_all_values()[1:]
                     if all(t in norm(" ".join(r)) for t in toks)]
        except Exception:
            found = []
        if found:
            out.append(f"\n{title}: {len(found)}")
            for r in found[-5:][::-1]:
                r = r + [""] * 14
                out.append("• " + " | ".join(str(r[c]) for c in cols if str(r[c]).strip()))
    return "\n".join(out)[:3900]


async def search_open(update, context):
    await ensure_roles()
    q = update.callback_query
    if update.effective_user.id not in ADMINS:
        if q:
            await q.answer("للمحاسبين بس", show_alert=True)
        return
    if q:
        await q.answer()
    context.user_data.pop("srch_wait", None)
    context.user_data.pop("srch_mode", None)
    mk = InlineKeyboardMarkup([[InlineKeyboardButton("🔎 بحث في القيود", callback_data="sr:txt"),
                                InlineKeyboardButton("💰 بحث في الأرصدة", callback_data="sr:bal")]])
    await update.effective_message.reply_text("تدوّر على إيه؟", reply_markup=mk)


async def search_off(update, context):
    q = update.callback_query
    context.user_data.pop("srch_wait", None)
    context.user_data.pop("srch_mode", None)
    await q.answer()
    await say(update, "تم. تفتح إيه؟", menu_markup())


# ================= العمل كمستخدم (المدير بس) =================
ACTING = {}       # {OWNER_ID: {"uid": رقم المستخدم, "name": اسمه}}
ACT_PASS_CB = re.compile(r"^(ok:|no:|u:|ob:|tq:|cx:|cq:|tp:|rq:|rj:|ib:|mn:|pd:|bc:|ed:|cd:|as:|rs:|gh:|ca:|sr:|tabdel:|pz:|sb:)")
ACT_PASS_TXT = re.compile(r"^/(me|actas|azkar|broadcast|buildform|cleardata|resetinbox|gltest|sheetreports|custodytabs|cleantabs)\b")
ROLE_AR = {"seller": "بياع", "sales_mgr": "مسؤول مبيعات", "install_mgr": "مدير تركيبات", "ceo": "CEO", "sm": "SM",
           "cash_main": "أمين الخزنة", "cash_sub": "أمين الخزنة الفرعية", "accountant": "محاسب", "operation": "Operation"}


def chat_of(uid):
    """لو المدير شغال كمستخدم، أي رسالة المفروض توصل للمستخدم ده بتتوجّه للمدير."""
    return OWNER_ID if ACTING.get(OWNER_ID, {}).get("uid") == uid else uid


def _act_banner_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ رجوع لحسابي", callback_data="as:off")]])


async def act_pre(update, context):
    """قبل أي handler: لو المدير شغال كمستخدم، بنخلّي البوت يشوفه هو المستخدم ده."""
    u = update.effective_user
    if not u:
        return
    REAL_UID.set(u.id)
    st = ACTING.get(OWNER_ID)
    if u.id != OWNER_ID or not st:
        return
    app = context.application
    ud_o = app.user_data.get(OWNER_ID) or {}
    ud_t = app.user_data.get(st["uid"]) or {}
    q, m = update.callback_query, update.message
    if q is not None:
        data = q.data or ""
        if ACT_PASS_CB.match(data):
            return
        if data.startswith("f:") and ud_o.get("form") and not ud_t.get("form"):
            return
    elif m is not None:
        txt = (m.text or "").strip()
        if ACT_PASS_TXT.match(txt):
            return
        is_start = txt in (".", MENU_BTN) or txt.startswith("/start")
        waiting = ud_o.get("rej") or ud_o.get("mn_wait") or ud_o.get("bc_wait") or ud_o.get("srch_wait") or ud_o.get("rw") or (ud_o.get("ed") or {}).get("wait")
        if not is_start and (waiting or (ud_o.get("form") and not ud_t.get("form"))):
            return
        if is_start:
            if st.pop("skip_banner", False):
                pass
            else:
                try:
                    await context.bot.send_message(OWNER_ID, f"👤 شغال دلوقتي كـ {st['name']}. كل اللي تعمله هيتسجل باسمه.", reply_markup=_act_banner_kb())
                except Exception as e:
                    logging.warning("banner failed: %s", e)
    try:
        object.__setattr__(u, "id", st["uid"])
    except Exception as e:
        logging.warning("acting-as not supported by this library version: %s", e)
        ACTING.pop(OWNER_ID, None)


async def act_list(update, context):
    q = update.callback_query
    if update.effective_user.id != OWNER_ID:
        if q:
            await q.answer("للمدير بس", show_alert=True)
        return
    if q:
        await q.answer()
    await ensure_roles()
    users = [(i, n, r) for i, n, r in await run(read_users) if i != OWNER_ID]
    seen, rows, btns = set(), [], []
    for i, n, r in users:
        if i in seen:
            continue
        seen.add(i)
        lbl = n + (f" ({ROLE_AR[r]})" if r in ROLE_AR else "")
        btns.append(InlineKeyboardButton(lbl[:40], callback_data=f"as:u:{i}"))
    rows = pairs(btns[:90])
    rows.append([InlineKeyboardButton("⬅️ رجوع", callback_data="gh:menu")])
    txt = "👥 اختار المستخدم اللي عايز تشتغل كأنك هو:" if btns else "مفيش مستخدمين مسجلين في Users_IDs."
    mk = InlineKeyboardMarkup(rows)
    if q:
        await say(update, txt, mk)
    else:
        await update.message.reply_text(txt, reply_markup=mk)


async def act_cb(update, context):
    q = update.callback_query
    if q.from_user.id != OWNER_ID:
        await q.answer("للمدير بس", show_alert=True)
        return
    p = q.data.split(":")
    if p[1] == "list":
        return await act_list(update, context)
    if p[1] == "off":
        await q.answer()
        was = ACTING.pop(OWNER_ID, None)
        context.application.user_data.get(OWNER_ID, {}).clear()
        await context.bot.send_message(OWNER_ID, "رجعت لحسابك ✅" + (f" (خرجت من {was['name']})" if was else ""), reply_markup=menu_markup())
        return
    if p[1] == "u":
        uid = int(p[2])
        await ensure_roles()
        users = await run(read_users)
        hit = next(((n, r) for i, n, r in users if i == uid), None)
        if not hit:
            await q.answer("المستخدم مش موجود", show_alert=True)
            return
        await q.answer()
        context.application.user_data.get(OWNER_ID, {}).clear()
        ACTING[OWNER_ID] = {"uid": uid, "name": hit[0], "skip_banner": True}
        await context.bot.send_message(OWNER_ID, f"👤 شغال دلوقتي كـ {hit[0]}" + (f" ({ROLE_AR[hit[1]]})" if hit[1] in ROLE_AR else "")
                                       + ". كل اللي تعمله هيتسجل باسمه.", reply_markup=_act_banner_kb())
        upd = Update.de_json({"update_id": int(time.time() * 1000) % 2147483647,
                              "message": {"message_id": q.message.message_id + 1, "date": int(time.time()),
                                          "chat": {"id": q.message.chat_id, "type": "private"},
                                          "from": {"id": OWNER_ID, "is_bot": False, "first_name": q.from_user.first_name or "owner"},
                                          "text": "."}}, context.bot)
        await context.application.update_queue.put(upd)


async def act_off_cmd(update, context):
    if update.effective_user.id != OWNER_ID:
        return
    was = ACTING.pop(OWNER_ID, None)
    context.application.user_data.get(OWNER_ID, {}).clear()
    await update.message.reply_text("رجعت لحسابك ✅" + (f" (خرجت من {was['name']})" if was else ""), reply_markup=menu_markup())


# ================= رسايل الصلاة على النبي والأذكار (بتتبعت تلقائي لكل المستخدمين) =================
AZKAR_OFF = os.environ.get("AZKAR_OFF", "").strip() == "1"
AZKAR_TIMES = [x.strip() for x in os.environ.get("AZKAR_TIMES", "06:30,13:00,17:30").split(",") if re.match(r"^\d{1,2}:\d{2}$", x.strip())]
AZKAR_STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "azkar_state.json")
AZKAR_LIST = [
    "سُبْحَانَ اللَّهِ وَبِحَمْدِهِ، سُبْحَانَ اللَّهِ الْعَظِيمِ",
    "لَا إِلَهَ إِلَّا اللَّهُ وَحْدَهُ لَا شَرِيكَ لَهُ، لَهُ الْمُلْكُ وَلَهُ الْحَمْدُ وَهُوَ عَلَى كُلِّ شَيْءٍ قَدِيرٌ",
    "أَسْتَغْفِرُ اللَّهَ وَأَتُوبُ إِلَيْهِ",
    "لَا حَوْلَ وَلَا قُوَّةَ إِلَّا بِاللَّهِ",
    "حَسْبِيَ اللَّهُ لَا إِلَهَ إِلَّا هُوَ عَلَيْهِ تَوَكَّلْتُ وَهُوَ رَبُّ الْعَرْشِ الْعَظِيمِ",
    "بِسْمِ اللَّهِ الَّذِي لَا يَضُرُّ مَعَ اسْمِهِ شَيْءٌ فِي الْأَرْضِ وَلَا فِي السَّمَاءِ وَهُوَ السَّمِيعُ الْعَلِيمُ",
    "سُبْحَانَ اللَّهِ، وَالْحَمْدُ لِلَّهِ، وَلَا إِلَهَ إِلَّا اللَّهُ، وَاللَّهُ أَكْبَرُ",
]


def azkar_text(slot, weekday):
    hh = int(slot.split(":")[0])
    z = AZKAR_LIST[weekday % len(AZKAR_LIST)]
    if hh < 11:
        head = "🌅 أذكار الصباح"
    elif hh < 15:
        head = "🌿 صلي على محمد ﷺ"
    else:
        head = "🌇 أذكار المساء"
    extra = "\n\nجمعة مباركة 🌸 أكثر من الصلاة على النبي ﷺ واقرأ سورة الكهف." if weekday == 4 and hh < 15 else ""
    return f"{head}\n\nاللهم صلِّ وسلِّم على نبينا محمد ﷺ ❀\n\n{z}{extra}"


def azkar_recipients():
    ids = {i for i, _, _ in read_users()} | set(ADMINS) | set(CEO_IDS) | set(CASH_MAIN_IDS) | set(CASH_SUB_IDS) | set(OPERATION_IDS)
    if GOMAA_ID:
        ids.add(GOMAA_ID)
    ids.add(OWNER_ID)
    return sorted(i for i in ids if i)


async def azkar_send(bot, text):
    await ensure_roles()
    ids = await run(azkar_recipients)
    ok = 0
    for uid in ids:
        try:
            await bot.send_message(uid, text)
            ok += 1
        except Exception as e:
            logging.info("azkar to %s failed: %s", uid, e)
        await asyncio.sleep(0.05)
    return ok, len(ids)


async def azkar_loop(app):
    await asyncio.sleep(20)
    while True:
        try:
            now = _cairo_now()
            try:
                with open(AZKAR_STATE, encoding="utf-8") as fh:
                    st = json.load(fh)
            except Exception:
                st = {}
            today = now.strftime("%Y-%m-%d")
            if st.get("date") != today:
                st = {"date": today, "sent": []}
            for slot in AZKAR_TIMES:
                h, mi = map(int, slot.split(":"))
                mins = now.hour * 60 + now.minute - (h * 60 + mi)
                if slot not in st["sent"] and 0 <= mins <= 90:
                    st["sent"].append(slot)
                    with open(AZKAR_STATE, "w", encoding="utf-8") as fh:
                        json.dump(st, fh)
                    await azkar_send(app.bot, azkar_text(slot, now.weekday()))
        except Exception as e:
            logging.warning("azkar loop error: %s", e)
        await asyncio.sleep(40)


async def azkar_cmd(update, context):
    if update.effective_user.id != OWNER_ID:
        return
    now = _cairo_now()
    ok, total = await azkar_send(context.bot, azkar_text("06:30" if now.hour < 11 else ("13:00" if now.hour < 15 else "17:30"), now.weekday()))
    await update.message.reply_text(f"✅ اتبعتت لـ {ok} من {total}. (اللي ماوصلهمش لسه ماداسوش /start على البوت)")


# ================= مسح التابات اللي ملهاش لازمة =================
KEEP_TABS = {GL_TAB, "Pending", "Lists", USERS_TAB, INCOME_TAB, "Suppliers_Log", "Expenses_Log", "Income_Log", "Custody_Log", "Advances_Log",
             "Assets_Log", CASH_PEND_TAB, "الخزنة الرئيسية", "الخزنة الفرعية", TQ_TAB, NOTES_TAB, "Deleted_Entries", REPORTS_TAB,
             CASH_COUNT_TAB, CUS_TAB_GOMAA, CUS_TAB_HASSAN}


def stale_tabs():
    return [w.title for w in sheet.worksheets() if w.title not in KEEP_TABS and not w.title.startswith("تسوية عهدة")]


async def cleantabs_cmd(update, context):
    if update.effective_user.id != OWNER_ID:
        return
    tabs = await run(stale_tabs)
    context.application.bot_data["stale_tabs"] = tabs
    if not tabs:
        await update.message.reply_text("مفيش تابات زيادة. كله شغال ✅")
        return
    rows = [[InlineKeyboardButton(f"🗑 {t}", callback_data=f"tabdel:{i}")] for i, t in enumerate(tabs[:40])]
    await update.message.reply_text("دي تابات البوت مش بيستخدمها. اضغط على اللي عايز تمسحه (هيطلبلك تأكيد):", reply_markup=InlineKeyboardMarkup(rows))


async def tabdel_cb(update, context):
    q = update.callback_query
    if q.from_user.id != OWNER_ID:
        await q.answer("للمدير بس", show_alert=True)
        return
    p = q.data.split(":")
    tabs = context.application.bot_data.get("stale_tabs", [])
    try:
        name = tabs[int(p[1])]
    except Exception:
        await q.answer("القايمة قديمة، ابعت /cleantabs تاني", show_alert=True)
        return
    await q.answer()
    if len(p) == 2:
        await q.edit_message_text(f"تمسح تاب «{name}» نهائي؟", reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("أيوه امسح", callback_data=f"tabdel:{p[1]}:y"), InlineKeyboardButton("لأ", callback_data="tabdel:x:n")]]))
        return
    try:
        await run(lambda: sheet.del_worksheet(sheet.worksheet(name)))
        await q.edit_message_text(f"🗑 اتمسح تاب «{name}».")
    except Exception as e:
        await q.edit_message_text(f"ماقدرتش أمسحه: {e}")


async def tabdel_no(update, context):
    await update.callback_query.answer()
    await update.callback_query.edit_message_text("تمام، مفيش حاجة اتمسحت.")


async def search_cmd(update, context):
    return await search_open(update, context)


async def treasury_cmd(update, context):
    return await cash_owner_menu(update, context)


async def ed_text_search(update, context):
    """نص بحث المدير/المحاسب (بيتنادى من ed_text)."""
    txt = (update.message.text or "").strip()
    context.user_data["srch_wait"] = True
    mode = context.user_data.get("srch_mode", "txt")
    res = await run(bal_view, mode[4:], txt) if mode.startswith("bal:") else await run(gl_search, txt)
    await update.message.reply_text(res, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("إنهاء البحث", callback_data="sr:off")]]))
    raise ApplicationHandlerStop




# ================= مزامنة اليومية (إكسل على Drive) مع GL_Entries =================
# إضافة قيود فقط. اليومية بتتقرا قراءة فقط، ومفيش أي تعديل في ملف الإكسل.
# في ملف .env:  JOURNAL_FILE_ID=<ID الملف من لينك Drive>   و (اختياري)  JOURNAL_SHEET=GL   و  SYNC_FROM=2026-11-01
JOURNAL_FILE_ID = os.environ.get("JOURNAL_FILE_ID", "").strip()
JOURNAL_SHEET = os.environ.get("JOURNAL_SHEET", "GL").strip()
SYNC_FROM = os.environ.get("SYNC_FROM", "2026-11-01").strip()
SYNC_OUT_TAB = "للنقل للإكسل"
DUP_TAB = "فحص التكرار"
SYNC_COLS = 17      # User .. Credit (عمود Control في الإكسل بيتساب)


def _sy_text(x):
    s = "" if x is None else str(x)
    for a, b in (("ى", "ي"), ("أ", "ا"), ("إ", "ا"), ("آ", "ا"), ("ة", "ه")):
        s = s.replace(a, b)
    return re.sub(r"\s+", " ", s).strip().casefold()


def _sy_date(x):
    if x is None or x == "":
        return ""
    if hasattr(x, "strftime"):
        return x.strftime("%Y-%m-%d")
    if isinstance(x, (int, float)):
        try:
            from openpyxl.utils.datetime import from_excel
            return from_excel(x).strftime("%Y-%m-%d")
        except Exception:
            return ""
    d = parse_date(x)
    return d.strftime("%Y-%m-%d") if d else ""


def _sy_num(x):
    if x is None or x == "":
        return 0.0
    if isinstance(x, (int, float)):
        return round(float(x), 2)
    return round(num(x), 2)


def _sy_clean(r):
    r = list(r)[:SYNC_COLS]
    return r + [None] * (SYNC_COLS - len(r))


def _sy_key(r):
    """بصمة السطر: التاريخ + المستويات 1-4 + اسم العميل + مدين + دائن (من غير رقم القيد والبيان والمشروع لأنهم بيختلفوا بين الجهتين)."""
    return "|".join([_sy_date(r[1])] + [_sy_text(r[i]) for i in (4, 5, 6, 7, 13)]
                    + [f"{_sy_num(r[15]):.2f}", f"{_sy_num(r[16]):.2f}"])


def _sy_key2(r):
    return f"{_sy_date(r[1])}|{_sy_num(r[15]):.2f}|{_sy_num(r[16]):.2f}"


def _sy_full(r):
    out = []
    for i, c in enumerate(r):
        if i == 1:
            out.append(_sy_date(c))
        elif i in (15, 16):
            out.append(f"{_sy_num(c):.2f}")
        else:
            out.append(_sy_text(c))
    return "|".join(out)


def _sy_ok(r):
    d = _sy_date(r[1])
    return bool(d) and d >= SYNC_FROM and (_sy_num(r[15]) or _sy_num(r[16]))


def sy_bot_rows():
    vals = sheet.worksheet(GL_TAB).get_all_values()
    return [(i + 1, _sy_clean(v)) for i, v in enumerate(vals) if i >= 1 and any(str(c).strip() for c in v)]


def sy_excel_rows():
    """بيحمّل اليومية من Drive قراءة فقط (من غير ما يلمس الملف) وبيرجّع [(رقم الصف, 17 عمود)]."""
    from googleapiclient.http import MediaIoBaseDownload
    from openpyxl import load_workbook
    svc = _drive()
    meta = svc.files().get(fileId=JOURNAL_FILE_ID, fields="name,modifiedTime,size", supportsAllDrives=True).execute()
    buf = io.BytesIO()
    dl = MediaIoBaseDownload(buf, svc.files().get_media(fileId=JOURNAL_FILE_ID, supportsAllDrives=True), chunksize=8 * 1024 * 1024)
    done = False
    while not done:
        _, done = dl.next_chunk()
    buf.seek(0)
    wb = load_workbook(buf, read_only=True, data_only=True)
    names = [JOURNAL_SHEET] + [n for n in wb.sheetnames if n != JOURNAL_SHEET]
    for nm in names:
        if nm not in wb.sheetnames:
            continue
        ws = wb[nm]
        head, c0, out = None, 0, []
        for n, row in enumerate(ws.iter_rows(values_only=True), start=1):
            if head is None:
                if n > 40:
                    break
                cells = [str(c).strip() if c is not None else "" for c in row]
                for j in range(len(cells) - 1):
                    if cells[j] == "User" and cells[j + 1] == "Date":
                        head, c0 = n, j
                        break
                continue
            out.append((n, _sy_clean(list(row)[c0:c0 + SYNC_COLS])))
        if head is not None:
            wb.close()
            return out, meta
    wb.close()
    raise RuntimeError("ملقيتش صف العناوين (User / Date ...) في ملف اليومية. اتأكد من اسم الشيت في JOURNAL_SHEET.")


def sy_diff():
    from collections import defaultdict
    xl_all, meta = sy_excel_rows()
    bot = [(n, r) for n, r in sy_bot_rows() if _sy_ok(r)]
    xl = [(n, r) for n, r in xl_all if _sy_ok(r)]
    bk, xk = defaultdict(list), defaultdict(list)
    for it in bot:
        bk[_sy_key(it[1])].append(it)
    for it in xl:
        xk[_sy_key(it[1])].append(it)
    miss_x, miss_b = [], []                     # ناقص في الإكسل / ناقص في البوت
    for k, v in bk.items():
        extra = len(v) - len(xk.get(k, []))
        if extra > 0:
            miss_x += v[-extra:]
    for k, v in xk.items():
        extra = len(v) - len(bk.get(k, []))
        if extra > 0:
            miss_b += v[-extra:]
    # نفس التاريخ والمبلغ بس الحسابات مختلفة = غالبًا اتعدّل في جهة واحدة: مراجعة، مش نقل (عشان ما يتكررش)
    by2 = defaultdict(list)
    for it in miss_b:
        by2[_sy_key2(it[1])].append(it)
    review, keep_x, used = [], [], set()
    for it in miss_x:
        cand = by2.get(_sy_key2(it[1]))
        if cand:
            o = cand.pop()
            used.add(o[0])
            review.append((it, o))
        else:
            keep_x.append(it)
    keep_b = [it for it in miss_b if it[0] not in used]
    keep_x.sort(key=lambda t: t[0])
    keep_b.sort(key=lambda t: t[0])
    return {"bot_n": len(bot), "xl_n": len(xl), "keep_x": keep_x, "keep_b": keep_b, "review": review,
            "mod": (meta.get("modifiedTime") or "")[:16].replace("T", " "), "name": meta.get("name", "")}


def sy_dups(rows):
    from collections import defaultdict
    g = defaultdict(list)
    for n, r in rows:
        if _sy_ok(r):
            g[_sy_full(r)].append((n, r))
    return [v for v in g.values() if len(v) > 1]


def _sy_out(r):
    row = []
    for i, c in enumerate(r):
        if i == 1:
            row.append(_sy_date(c))
        elif i in (15, 16):
            row.append(_sy_num(c) or "")
        elif c is None:
            row.append("")
        elif isinstance(c, float) and c == int(c):
            row.append(int(c))
        else:
            row.append(c)
    return row


def _sy_line(r):
    amt = _sy_num(r[15]) or _sy_num(r[16])
    side = "مدين" if _sy_num(r[15]) else "دائن"
    acc = " / ".join(str(x) for x in (r[6], r[7], r[13]) if x not in (None, ""))
    return f"{_sy_date(r[1])} | {acc or '—'} | {side} {amt:g}"


def sy_apply(d):
    """بيكتب: قيود الإكسل الناقصة في GL_Entries (إضافة بس)، وقيود البوت الناقصة في تاب «للنقل للإكسل»."""
    if d["keep_b"]:
        gl_append([_sy_out(r) for _, r in d["keep_b"]])
    ws, _new = _tab(SYNC_OUT_TAB, 1000, SYNC_COLS)
    header = sheet.worksheet(GL_TAB).row_values(1)[:SYNC_COLS]
    ws.clear()
    ws.update("A1", [header] + [_sy_out(r) for _, r in d["keep_x"]], value_input_option="USER_ENTERED")


def sy_preview(d):
    t = [f"🔄 معاينة المزامنة (من {SYNC_FROM})",
         f"قيود البوت: {d['bot_n']} سطر | اليومية: {d['xl_n']} سطر",
         f"آخر حفظ لملف اليومية على Drive: {d['mod']} (UTC)", ""]
    t.append(f"➡️ ناقصة في اليومية: {len(d['keep_x'])} سطر (هتتحط في تاب «{SYNC_OUT_TAB}» وإنت تنسخها)")
    t += [" • " + _sy_line(r) for _, r in d["keep_x"][:8]]
    t.append(f"\n⬅️ ناقصة في البوت: {len(d['keep_b'])} سطر (هتتضاف في GL_Entries)")
    t += [" • " + _sy_line(r) for _, r in d["keep_b"][:8]]
    if d["review"]:
        t.append(f"\n⚠️ محتاجة مراجعة: {len(d['review'])} (نفس التاريخ والمبلغ بحسابات مختلفة، مش هتتنقل)")
        t += [f" • بوت صف {a[0]}: {_sy_line(a[1])}\n   إكسل صف {b[0]}: {_sy_line(b[1])}" for a, b in d["review"][:5]]
    return cut_text("\n".join(t))


def sy_dup_run():
    bot_rows = sy_bot_rows()
    xl_rows, _ = sy_excel_rows()
    out = [["المصدر", "رقم الصف", "التاريخ", "البيان", "المستوى3", "العميل", "مدين", "دائن", "عدد التكرار"]]
    n_groups = 0
    for src, rows in (("البوت GL_Entries", bot_rows), ("اليومية الإكسل", xl_rows)):
        for grp in sy_dups(rows):
            n_groups += 1
            for n, r in grp:
                out.append([src, n, _sy_date(r[1]), r[3] or "", r[6] or "", r[13] or "",
                            _sy_num(r[15]) or "", _sy_num(r[16]) or "", len(grp)])
    ws, _new = _tab(DUP_TAB, 1000, 9)
    ws.clear()
    ws.update("A1", out, value_input_option="USER_ENTERED")
    return n_groups, len(out) - 1


async def sync_cb(update, context):
    await ensure_roles()
    q = update.callback_query
    if q.from_user.id not in ADMINS:
        await q.answer("للمحاسبين بس", show_alert=True)
        return
    act = q.data.split(":")[1]
    if act == "no":
        context.application.bot_data.pop("sy", None)
        await q.answer()
        await q.edit_message_text("اتلغت المزامنة، مفيش حاجة اتكتبت.")
        return
    if not JOURNAL_FILE_ID:
        await q.answer("حط JOURNAL_FILE_ID في ملف .env الأول", show_alert=True)
        return
    await q.answer()
    wait = await q.message.reply_text("⏳ بقرأ اليومية من Drive (قراءة فقط)... ممكن تاخد دقيقة.")
    try:
        if act == "dup":
            groups, lines = await run(sy_dup_run)
            await wait.edit_text(f"🧪 فحص التكرار خلص.\nمجموعات مشبوهة: {groups} ({lines} سطر)\nالتفاصيل في تاب «{DUP_TAB}» (سطور متطابقة في كل الأعمدة)." if groups
                                 else "🧪 فحص التكرار: مفيش أي سطور متكررة في البوت ولا في اليومية ✅")
            return
        d = await run(sy_diff)
        if act == "run":
            if not (d["keep_x"] or d["keep_b"]):
                msg = "✅ الاتنين متطابقين، مفيش حاجة تتنقل." + (f"\n⚠️ فيه {len(d['review'])} حالة محتاجة مراجعة (نفس التاريخ والمبلغ بحسابات مختلفة)." if d["review"] else "")
                await wait.edit_text(msg)
                return
            import uuid
            sid = uuid.uuid4().hex[:6]
            context.application.bot_data["sy"] = {"id": sid, "n": (len(d["keep_x"]), len(d["keep_b"]), len(d["review"]))}
            await wait.edit_text(sy_preview(d), reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ نفّذ المزامنة", callback_data=f"sy:go:{sid}"),
                InlineKeyboardButton("إلغاء", callback_data="sy:no")]]))
        elif act == "go":
            st = context.application.bot_data.get("sy")
            if not st or st["id"] != q.data.split(":")[2]:
                await wait.edit_text("المعاينة قديمة، اضغط «🔄 مزامنة» من جديد.")
                return
            if st["n"] != (len(d["keep_x"]), len(d["keep_b"]), len(d["review"])):
                context.application.bot_data.pop("sy", None)
                await wait.edit_text("⚠️ البيانات اتغيرت من وقت المعاينة. اضغط «🔄 مزامنة» من جديد وراجع المعاينة.")
                return
            await run(sy_apply, d)
            context.application.bot_data.pop("sy", None)
            await wait.edit_text(f"تمت المزامنة ✅\n• اتضاف في GL_Entries: {len(d['keep_b'])} سطر\n• جاهز للنسخ في تاب «{SYNC_OUT_TAB}»: {len(d['keep_x'])} سطر"
                                 + (f"\n• محتاجة مراجعة (ما اتنقلتش): {len(d['review'])}" if d["review"] else ""))
    except Exception as e:
        logging.warning("sync failed: %s", e)
        hint = "\nاتأكد إن ملف اليومية متشارك مع إيميل الـ Service Account." if "404" in str(e) or "403" in str(e) else ""
        await wait.edit_text(f"المزامنة فشلت ({type(e).__name__}): {e}{hint}")



# ================= إعادة بناء كل تابات المتابعة من GL_Entries (/rebuildtabs) =================
# بتبني: الخزنة الرئيسية والفرعية، Suppliers_Log، Expenses_Log، Income_Log، Assets_Log، Custody_Log، Advances_Log، وتابين العهدة.
# بتمسح صفوف كل تاب وتكتبها من الأول (العناوين والجداول الملخصة جنب العهد والسلف بتفضل زي ما هي).
RB_LOGS = ("Suppliers_Log", "Expenses_Log", "Income_Log", "Assets_Log", "Custody_Log", "Advances_Log")


def rb_rows(rows, L):
    inc_set = {(a, b) for a, b, _ in income_accounts()}
    out = {k: [] for k in RB_LOGS}
    for r in rows:
        dr, cr = num(r[15]), num(r[16])
        if not (dr or cr):
            continue
        amt = dr or cr
        l1, l2, l3, l4 = (str(r[i]).strip() for i in (4, 5, 6, 7))

        def base(kind, party):
            return ["", r[1], r[0], kind, party, amt, "", r[3], ""]
        if l2 == L["l2_cash"]:
            out.setdefault("cash:" + cash_sheet_name(l3), []).append([r[1], r[2], r[3], cr or "", dr or "", "", "", r[0], ""])
        if l2 == CUS_L2 and l3 in CUSTODY_TYPES:
            out["Custody_Log"].append(base("صرف عهدة" if dr else "تسوية عهدة", l4))
        elif l2 == CUS_L2 and l3 == ADV_L3:
            out["Advances_Log"].append(base("سلفة" if dr else "رد سلفة", l4))
        if (l1, l2) == (SUP_L1, SUP_L2) and dr:
            out["Suppliers_Log"].append(base("موردين", l3))
        if (l1, l2) == (ASSET_L1, ASSET_L2) and dr:
            out["Assets_Log"].append(base("شراء أصل", l4 or l3))
        if dr and l1 in EXP_L1_LIST:
            out["Expenses_Log"].append(base("مصروف", l4 or l3 or l2))
        if cr and (l1, l2) in inc_set and (l1, l2) != (DISC_L1, DISC_L2):
            out["Income_Log"].append(base("إيراد", l3 or l2))
    return out


def _rb_write(ws, rows, first, lastcol):
    ws.batch_clear([f"A{first}:{lastcol}{ws.row_count}"])
    if rows:
        need = first + len(rows) - 1
        if need > ws.row_count:
            ws.add_rows(need - ws.row_count + 100)
        ws.update(f"A{first}", rows, value_input_option="USER_ENTERED")


def _rb_emp_tab(tab, t_taken, t_settled, head_taken, head_settled):
    ws, new = _tab(tab, 1000, 14)
    if new:
        ws.update("A1", [LOG_HEADER])
        ws.update("J1", [["الموظف", head_taken, head_settled, "الفرق"]])
        crit = 'E:E,IF(J2:J="الإجمالي","*",J2:J)'
        taken = f'SUMIFS(F:F,{crit},D:D,"{t_taken}")'
        settled = "+".join(f'SUMIFS(F:F,{crit},D:D,"{t}")' for t in t_settled)
        ws.update("J2", [['={SORT(UNIQUE(FILTER(E2:E,E2:E<>"")));"الإجمالي"}']], value_input_option="USER_ENTERED")
        ws.update("K2", [[f'=ARRAYFORMULA(IF(J2:J="","",{taken}))']], value_input_option="USER_ENTERED")
        ws.update("L2", [[f'=ARRAYFORMULA(IF(J2:J="","",{settled}))']], value_input_option="USER_ENTERED")
        ws.update("M2", [['=ARRAYFORMULA(IF(J2:J="","",K2:K-L2:L))']], value_input_option="USER_ENTERED")
    return ws


def rb_preview():
    _GL_CACHE["t"] = 0
    out = rb_rows(read_gl(), read_lists())
    return {k: len(v) for k, v in out.items()}


def rb_apply():
    _GL_CACHE["t"] = 0
    out = rb_rows(read_gl(), read_lists())
    for tab in ("Suppliers_Log", "Expenses_Log", "Income_Log", "Assets_Log"):
        ws, new = _tab(tab, 1000, 14)
        if new:
            ws.update("A1", [LOG_HEADER])
        _rb_write(ws, out[tab], 2, "I")
    _rb_write(_rb_emp_tab("Custody_Log", "صرف عهدة", ("تسوية عهدة", "رد عهدة"), "العهدة", "التسوية"), out["Custody_Log"], 2, "I")
    _rb_write(_rb_emp_tab("Advances_Log", "سلفة", ("رد سلفة",), "السلفة", "المسترد"), out["Advances_Log"], 2, "I")
    for key in [k for k in out if k.startswith("cash:")]:
        ws, new = _tab(key[5:], 2000, 9)
        if new:
            ws.update("A1", [CASH_HEADER])
            ws.update("A2", [["", "", "رصيد أول المدة (اكتبه في عمود إيرادات)", 0, 0, "=SUM($E$2:E2)-SUM($D$2:D2)", "", "", ""]],
                      value_input_option="USER_ENTERED")
        rows = out[key]
        for i, row in enumerate(rows, start=3):
            row[5] = f"=SUM($E$2:E{i})-SUM($D$2:D{i})"
        _rb_write(ws, rows, 3, "I")
    custody_tabs_rebuild()
    return {k: len(v) for k, v in out.items()}


def _rb_text(c, head):
    cash = "".join(f"• {k[5:]}: {v} حركة\n" for k, v in c.items() if k.startswith("cash:"))
    return (f"{head}\n{cash}• Suppliers_Log: {c['Suppliers_Log']}\n"
            f"• Expenses_Log: {c['Expenses_Log']}\n• Income_Log: {c['Income_Log']}\n• Assets_Log: {c['Assets_Log']}\n"
            f"• Custody_Log: {c['Custody_Log']}\n• Advances_Log: {c['Advances_Log']}\n• تابين العهدة (م جمعه / م محمد حسن)")


async def rebuild_cmd(update, context):
    if update.effective_user.id != OWNER_ID:
        return
    wait = await update.message.reply_text("⏳ بحسب اللي هيتبني من GL_Entries...")
    try:
        c = await run(rb_preview)
    except Exception as e:
        await wait.edit_text(f"فشل ({type(e).__name__}): {e}")
        return
    await wait.edit_text(_rb_text(c, "🔧 هيتمسح محتوى التابات دي وهيتكتب من القيود من الأول:") +
                         "\n\n⚠️ المرفقات وطريقة الدفع ووقت التسجيل اللي كانت متسجلة في التابات دي هتضيع. اعمل نسخة من الشيت الأول (File > Make a copy).",
                         reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ نفّذ", callback_data="rb:go"),
                                                             InlineKeyboardButton("إلغاء", callback_data="rb:no")]]))


async def rebuild_cb(update, context):
    q = update.callback_query
    if q.from_user.id != OWNER_ID:
        await q.answer("غير مسموح", show_alert=True)
        return
    await q.answer()
    if q.data == "rb:no":
        await q.edit_message_text("اتلغت، مفيش حاجة اتغيرت.")
        return
    await q.edit_message_text("⏳ ببني التابات... ممكن تاخد كذا دقيقة.")
    try:
        c = await run(rb_apply)
        await q.edit_message_text(_rb_text(c, "تم بناء التابات من القيود ✅"))
    except Exception as e:
        logging.warning("rebuild failed: %s", e)
        await q.edit_message_text(f"فشل ({type(e).__name__}): {e}")



# ================= تحديث بلصق + سحب الخزنة الفرعية (المدير) =================
# 1) "📥 تحديث بلصق": تلصق قيود في GL_Entries، تدوس الزرار، تكتب رقم أول صف لصقته، البوت يفحصها ويحدّث كل التابات.
# 2) "🏦 سحب الخزنة الفرعية": تكتب رقم السطر في شيت الخزنة الفرعية، البوت يسحب العمليات من السطر ده
#    ويبعتهالك واحدة واحدة بنفس أزرار اعتماد الخزنة (اعتماد واختيار الحساب / من غير قيد / رفض).
# في ملف .env (اختياري): SUBSHEET_ID=...  SUBSHEET_GID=...
SUBSHEET_ID = os.environ.get("SUBSHEET_ID", "1_jCzbIf9v--PCXELg2WBpfaYdBxBwounfcZ_wiI38nw").strip()
SUBSHEET_GID = os.environ.get("SUBSHEET_GID", "1484342").strip()
SB_MAX = 40


def _rw_int(t):
    t = (t or "").translate(AR_DIGITS).strip()
    return int(t) if t.isdigit() and int(t) >= 2 else None


def _rw_kb(code):
    return InlineKeyboardMarkup([[InlineKeyboardButton("إلغاء", callback_data=f"{code}:no")]])


# ---------- تحديث بلصق ----------
def pz_scan(start):
    vals = sheet.worksheet(GL_TAB).get_all_values()
    total = len(vals)
    if start > total:
        return {"total": total, "empty": True}
    groups, cur = [], []
    for n in range(start, total + 1):
        v = list(vals[n - 1]) + [""] * (17 - len(vals[n - 1]))
        if not any(str(c).strip() for c in v):
            if cur:
                groups.append(cur)
                cur = []
            continue
        cur.append((n, v))
    if cur:
        groups.append(cur)
    lines = [x for g in groups for x in g if num(x[1][15]) or num(x[1][16])]
    dr = round(sum(num(v[15]) for _, v in lines), 2)
    cr = round(sum(num(v[16]) for _, v in lines), 2)
    bad = []
    for g in groups:
        d = round(sum(num(v[15]) for _, v in g), 2)
        c = round(sum(num(v[16]) for _, v in g), 2)
        if abs(d - c) > 0.009:
            bad.append((g[0][0], d, c))
    dates = sorted({_sy_date(v[1]) for _, v in lines if _sy_date(v[1])})
    no_acc = [n for n, v in lines if not str(v[4]).strip()]
    return {"total": total, "empty": False, "n": len(lines), "groups": len(groups), "dr": dr, "cr": cr, "bad": bad,
            "d1": dates[0] if dates else "", "d2": dates[-1] if dates else "", "no_acc": no_acc}


def pz_text(start, s):
    if s["empty"]:
        return f"رقم الصف {start} أكبر من آخر صف في GL_Entries (آخر صف: {s['total']}). اكتب رقم تاني."
    t = [f"📥 تحديث بلصق (من صف {start} لحد آخر صف {s['total']})",
         f"• عدد الأسطر: {s['n']}  |  عدد القيود: {s['groups']}",
         f"• من تاريخ {s['d1'] or '—'} لحد {s['d2'] or '—'}",
         f"• إجمالي مدين: {s['dr']:,.2f}  |  إجمالي دائن: {s['cr']:,.2f}"]
    if abs(s["dr"] - s["cr"]) < 0.01 and not s["bad"]:
        t.append("✅ كل القيود متوازنة")
    else:
        t.append("⚠️ في قيود مش متوازنة (راجعها قبل ما تأكد):")
        t += [f"   صف {a}: مدين {b:,.2f} / دائن {c:,.2f}" for a, b, c in s["bad"][:6]]
    if s["no_acc"]:
        t.append("⚠️ أسطر من غير حساب (المستوى 1) في الصفوف: " + "، ".join(str(x) for x in s["no_acc"][:8]))
    t.append("\nلما تأكد: هيحدّث كل التابات (الخزنة الرئيسية والفرعية، الموردين، المصروفات، الإيرادات، الأصول، العهد، السلف) من القيود.")
    return cut_text("\n".join(t))


# ---------- سحب الخزنة الفرعية ----------
def _g(v, i):
    return v[i] if i is not None and 0 <= i < len(v) else ""


def sb_cols(vals):
    """بيدوّر على صف العناوين (بيان / تاريخ / إيراد / مصروف / رصيد / رقم الوصل)."""
    for r in vals[:8]:
        m = {}
        for i, c in enumerate(r):
            t = _sy_text(c)
            if not t:
                continue
            if "بيان" in t:
                m.setdefault("b", i)
            elif "تاريخ" in t:
                m.setdefault("d", i)
            elif "رصيد" in t:
                m.setdefault("bal", i)
            elif any(k in t for k in ("ايراد", "وارد", "استلام", "قبض")):
                m.setdefault("inc", i)
            elif any(k in t for k in ("مصروف", "منصرف", "صرف", "مدفوع")):
                m.setdefault("exp", i)
            elif "وصل" in t or t == "رقم":
                m.setdefault("v", i)
        if all(k in m for k in ("b", "d", "inc", "exp")):
            return m
    return None


def sb_parse(vals):
    cols = sb_cols(vals)
    rows = []
    for n, v in enumerate(vals, start=1):
        v = list(v)
        if cols:
            di, bi, ii, ei, bal_i, vi = cols["d"], cols["b"], cols["inc"], cols["exp"], cols.get("bal"), cols.get("v")
            d = _sy_date(_g(v, di))
            if not d:
                continue
            if vi is None:
                vi = next((j for j in (di - 1, di + 1) if str(_g(v, j)).strip().isdigit()), None)
        else:
            di = next((j for j, c in enumerate(v) if _sy_date(c)), None)
            if di is None:
                continue
            d = _sy_date(v[di])
            txt = [j for j, c in enumerate(v) if re.search(r"[\u0600-\u06FFA-Za-z]", str(c)) and _sy_date(c) == ""]
            after = [j for j in txt if j > di]
            if not after:
                continue
            bi = after[0]          # أول خلية فيها كلام بعد التاريخ = البيان
            ii, ei, bal_i = bi + 1, bi + 2, bi + 3
            vi = next((j for j in (di - 1, di + 1) if str(_g(v, j)).strip().isdigit()), None)
        a, b = num(_g(v, ii)), num(_g(v, ei))
        if not (a or b):
            continue
        balraw = str(_g(v, bal_i)).strip()
        rows.append({"n": n, "date": d, "voucher": str(_g(v, vi)).strip() if vi is not None else "",
                     "bayan": str(_g(v, bi)).strip(), "inc": a, "exp": b,
                     "bal": num(balraw) if balraw != "" else None})
    return rows


def _sb_bad(rows, swap):
    bad = []
    for k in range(1, len(rows)):
        p, r = rows[k - 1], rows[k]
        if p["bal"] is None or r["bal"] is None:
            continue
        i, e = (r["exp"], r["inc"]) if swap else (r["inc"], r["exp"])
        if abs(p["bal"] + i - e - r["bal"]) > 0.01:
            bad.append(r["n"])
    return bad


def sb_pull(start):
    sh = gspread.authorize(creds).open_by_key(SUBSHEET_ID)
    ws = next((w for w in sh.worksheets() if str(w.id) == SUBSHEET_GID), None)
    if ws is None:
        raise RuntimeError(f"مش لاقي التاب بالرقم gid={SUBSHEET_GID} في شيت الخزنة الفرعية.")
    vals = ws.get_all_values()
    rows = sb_parse(vals)
    bad0, bad1 = _sb_bad(rows, False), _sb_bad(rows, True)
    swap = len(bad1) < len(bad0)
    if swap:
        for r in rows:
            r["inc"], r["exp"] = r["exp"], r["inc"]
    bad = [n for n in (bad1 if swap else bad0) if n >= start]
    ops = [r for r in rows if r["n"] >= start]
    # اللي اتسحب قبل كده (نفس رقم الوصل والمبلغ في Cash_Pending للخزنة الفرعية)
    done = set()
    try:
        cp = sheet.worksheet(CASH_PEND_TAB).get_all_values()[1:]
        for r in cp:
            r = r + [""] * 14
            if "فرع" in norm(r[8]) and "رفض" not in r[10]:
                done.add((str(r[3]).strip(), round(num(r[5]), 2)))
    except gspread.WorksheetNotFound:
        pass
    new = [o for o in ops if (o["voucher"], round(o["inc"] or o["exp"], 2)) not in done]
    return {"last": len(vals), "ops": new, "skipped": len(ops) - len(new), "bad": bad, "swap": swap}


def _sb_line(o):
    kind = "إيراد" if o["inc"] else "مصروف"
    return f"صف {o['n']} | {o['date']} | {kind} {(o['inc'] or o['exp']):,.2f} | وصل {o['voucher'] or '—'} | {o['bayan'][:40]}"


def sb_text(start, s):
    ops = s["ops"]
    if not ops:
        extra = f"\n(في {s['skipped']} عملية اتسحبت قبل كده)" if s["skipped"] else ""
        return f"مفيش عمليات جديدة من صف {start} لحد آخر صف في الشيت ({s['last']}).{extra}"
    inc = [o for o in ops if o["inc"]]
    exp = [o for o in ops if o["exp"]]
    t = [f"🏦 الخزنة الفرعية (من صف {start})",
         f"لقيت {len(ops)} عملية جديدة:",
         f"• مصروف: {len(exp)} عملية، إجمالي {sum(o['exp'] for o in exp):,.2f}",
         f"• إيراد: {len(inc)} عملية، إجمالي {sum(o['inc'] for o in inc):,.2f}"]
    if s["skipped"]:
        t.append(f"(اتخطيت {s['skipped']} عملية اتسحبت قبل كده)")
    t.append("\nأول عمليتين:")
    t += [" • " + _sb_line(o) for o in ops[:2]]
    t.append("آخر عملية:")
    t.append(" • " + _sb_line(ops[-1]))
    if s["bad"]:
        t.append(f"\n⚠️ الرصيد في الشيت مش مطابق للحركة عند صف: {', '.join(str(x) for x in s['bad'][:6])}. راجع الشيت.")
    else:
        t.append("\n✅ الأرصدة في الشيت متطابقة مع الحركات.")
    if len(ops) > SB_MAX:
        t.append(f"\nهبعت أول {SB_MAX} عملية بس، وبعدها اضغط الزرار وابدأ من صف {ops[SB_MAX]['n']}.")
    t.append("هتوصلك العمليات واحدة واحدة، وتختار كل واحدة هتتقفل على إيه.")
    return cut_text("\n".join(t))


def sb_save(ops):
    ws, new = _tab(CASH_PEND_TAB, 1000, len(CASH_PEND_HEADER))
    if new or [h.strip() for h in ws.row_values(1)][:len(CASH_PEND_HEADER)] != CASH_PEND_HEADER:
        ws.update("A1", [CASH_PEND_HEADER])
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    pids, rows = [], []
    for o in ops:
        pid = "S" + uuid.uuid4().hex[:5]
        amt = o["inc"] or o["exp"]
        rows.append([pid, now, o["date"], _safe_cell(o["voucher"]), _safe_cell(o["bayan"]), amt,
                     "إيراد" if o["inc"] else "مصروف", "", "الخزنة الفرعية", "سحب من شيت الخزنة الفرعية",
                     "مستني اعتماد", "", "", ""])
        pids.append(pid)
    _append(ws, rows)
    return pids


async def rw_text(update, context):
    st = context.user_data.get("rw")
    if not st or not update.message or not update.message.text:
        return
    t = update.message.text.strip()
    if t in (".", MENU_BTN) or t.startswith("/"):
        context.user_data.pop("rw", None)
        return
    await ensure_roles()
    uid = update.effective_user.id
    if uid not in ADMINS or (st["k"] == "sb" and uid != OWNER_ID):
        return
    n = _rw_int(t)
    code = st["k"]
    if not n:
        await update.message.reply_text("اكتب رقم الصف بالأرقام بس (مثلًا 682).", reply_markup=_rw_kb(code))
        raise ApplicationHandlerStop
    context.user_data.pop("rw", None)
    wait = await update.message.reply_text("⏳ بقرا الشيت...")
    try:
        if code == "pz":
            s = await run(pz_scan, n)
            if s["empty"]:
                context.user_data["rw"] = {"k": "pz"}
                await wait.edit_text(pz_text(n, s), reply_markup=_rw_kb("pz"))
                raise ApplicationHandlerStop
            sid = uuid.uuid4().hex[:6]
            context.application.bot_data["pz"] = {"id": sid, "start": n, "n": s["n"]}
            await wait.edit_text(pz_text(n, s), reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ تأكيد وتحديث كل التابات", callback_data=f"pz:go:{sid}"),
                InlineKeyboardButton("إلغاء", callback_data="pz:no")]]))
        else:
            s = await run(sb_pull, n)
            if not s["ops"]:
                await wait.edit_text(sb_text(n, s))
            else:
                sid = uuid.uuid4().hex[:6]
                context.application.bot_data["sb"] = {"id": sid, "ops": s["ops"]}
                await wait.edit_text(sb_text(n, s), reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ ابعتهم لي", callback_data=f"sb:go:{sid}"),
                    InlineKeyboardButton("إلغاء", callback_data="sb:no")]]))
    except ApplicationHandlerStop:
        raise
    except Exception as e:
        logging.warning("rw failed: %s", e)
        hint = "\nاتأكد إن الشيت متشارك مع إيميل الـ Service Account." if code == "sb" and ("403" in str(e) or "404" in str(e)) else ""
        await wait.edit_text(f"فشل ({type(e).__name__}): {e}{hint}")
    raise ApplicationHandlerStop


async def rw_cb(update, context):
    await ensure_roles()
    q = update.callback_query
    uid = q.from_user.id
    p = q.data.split(":")
    code, act = p[0], p[1]
    if uid not in ADMINS or (code == "sb" and uid != OWNER_ID):
        await q.answer("الزرار ده للمدير بس" if code == "sb" else "للمحاسبين بس", show_alert=True)
        return
    await q.answer()
    if act == "no":
        context.user_data.pop("rw", None)
        context.application.bot_data.pop(code, None)
        await q.edit_message_text("اتلغى، مفيش حاجة اتغيرت.")
        return
    if act == "ask":
        context.user_data["rw"] = {"k": code}
        msg = ("📥 اكتب رقم أول صف لصقته في شيت البوت (GL_Entries):" if code == "pz"
               else "🏦 اكتب رقم السطر في شيت الخزنة الفرعية اللي عايز تبدأ منه:")
        await q.message.reply_text(msg, reply_markup=_rw_kb(code))
        return
    if act != "go":
        return
    st = context.application.bot_data.pop(code, None)
    if not st or st["id"] != p[2]:
        await q.edit_message_text("الرسالة قديمة. ابدأ من جديد من القايمة.")
        return
    if code == "pz":
        await q.edit_message_text("⏳ بحدّث كل التابات... ممكن تاخد كذا دقيقة.")
        try:
            s = await run(pz_scan, st["start"])
            if s["empty"] or s["n"] != st["n"]:
                await q.edit_message_text("⚠️ الشيت اتغير من وقت المعاينة. ابدأ من جديد من القايمة.")
                return
            c = await run(rb_apply)
            await q.edit_message_text(_rb_text(c, f"تم تحديث كل التابات من القيود ✅ (من صف {st['start']})"))
        except Exception as e:
            logging.warning("paste update failed: %s", e)
            await q.edit_message_text(f"فشل ({type(e).__name__}): {e}")
        return
    ops = st["ops"][:SB_MAX]
    await q.edit_message_text(f"⏳ بسجّل {len(ops)} عملية وببعتهالك...")
    try:
        pids = await run(sb_save, ops)
        for pid, o in zip(pids, ops):
            kind = "إيراد" if o["inc"] else "مصروف"
            m = await context.bot.send_message(
                OWNER_ID,
                f"📥 عملية من الخزنة الفرعية (صف {o['n']} في الشيت)\n"
                f"النوع: {kind}\nالمبلغ: {(o['inc'] or o['exp']):,.2f}\nالبيان: {o['bayan']}\n"
                f"التاريخ: {o['date']}\nرقم الوصل: {o['voucher'] or '—'}",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("اعتماد واختيار الحساب", callback_data=f"cx:{pid}:s")],
                    [InlineKeyboardButton("اعتماد من غير قيد", callback_data=f"cx:{pid}:n")],
                    [InlineKeyboardButton("رفض", callback_data=f"cx:{pid}:r")]]))
            msg_track(f"c{pid}", m)
            await asyncio.sleep(0.3)
        await q.edit_message_text(f"تم ✅ بعتلك {len(ops)} عملية. آخر صف اتسحب: {ops[-1]['n']}\n"
                                  f"المرة الجاية ابدأ من صف {ops[-1]['n'] + 1}.")
    except Exception as e:
        logging.warning("sub pull failed: %s", e)
        await q.edit_message_text(f"فشل ({type(e).__name__}): {e}")



# ================= بحث الأرصدة (موردين / عملاء / خزن / بنوك / عهد / سلف / أصول / أي حساب) =================
BAL_CATS = [("sup", "الموردين"), ("cli", "العملاء"), ("cash", "الخزن"), ("bank", "البنوك والمحافظ"),
            ("cus", "العهد"), ("adv", "السلف"), ("ast", "الأصول"), ("any", "أي حساب (بالاسم)")]
BAL_NAME = dict(BAL_CATS)


def bal_items(cat, toks=()):
    _GL_CACHE["t"] = 0
    rows = read_gl()
    side = ""
    if cat == "sup":
        d, side = balances(rows, SUP_L2, credit_normal=True), "علينا"
    elif cat == "cli":
        d, side = balances(rows, "العملاء"), "لنا"
    elif cat == "cash":
        d = balances(rows, "النقدية_بالخزينة")
    elif cat == "bank":
        d = balances(rows, "النقدية_بالبنوك")
    elif cat == "cus":
        d, side = cust_balances(rows, tuple(CUSTODY_TYPES)), "عليهم"
    elif cat == "adv":
        d, side = cust_balances(rows, (ADV_L3,)), "عليهم"
    elif cat == "ast":
        d = {}
        for r in rows:
            if r[5].strip() == ASSET_L2:
                k = r[7].strip() or r[6].strip() or ASSET_L2
                d[k] = d.get(k, 0) + num(r[15]) - num(r[16])
        d = {k: v for k, v in d.items() if abs(v) >= 0.5}
    else:
        d = {}
        for r in rows:
            k = " > ".join(str(x).strip() for x in r[4:8] if str(x).strip())
            d[k] = d.get(k, 0) + num(r[15]) - num(r[16])
        d = {k: v for k, v in d.items() if abs(v) >= 0.5}
    items = [(k, v) for k, v in d.items() if all(t in norm(k) for t in toks)]
    items.sort(key=lambda kv: -abs(kv[1]))
    return items, side


def bal_view(cat, qtext=""):
    toks = [norm(t) for t in qtext.translate(AR_DIGITS).split() if t.strip()]
    items, side = bal_items(cat, toks)
    title = f"💰 أرصدة: {BAL_NAME.get(cat, cat)}" + (f" | بحث: {qtext}" if qtext else "")
    out = [title, "━━━━━━━━━━━━━━━"]
    if not items:
        out.append("مفيش حاجة بالاسم ده." if toks else "مفيش أرصدة.")
        return "\n".join(out)
    for k, v in items[:40]:
        name = k.replace("_", " ")
        if cat == "any":
            out.append(f"• {name}: {abs(v):,.0f} {'مدين' if v > 0 else 'دائن'}")
        else:
            out.append(f"• {name}: {v:,.0f}")
    if len(items) > 40:
        out.append(f"... و{len(items) - 40} حساب تانيين. اكتب جزء من الاسم عشان تضيّق.")
    if cat != "any":
        out.append(f"\nالإجمالي: {sum(v for _, v in items):,.0f}" + (f" ({side})" if side else ""))
    return cut_text("\n".join(out))


BAL_HTML = r'''<!DOCTYPE html>
<html lang="ar" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>الأرصدة</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/pako/2.1.0/pako.min.js"></script>
<style>
:root{--bg:#FBF6EE;--card:#fff;--ink:#1F1F1F;--or:#F2A65A;--or2:#FCE7CF;--mut:#7A7266}
*{box-sizing:border-box}
html,body{margin:0;background:var(--bg);color:var(--ink);font-family:"Segoe UI",Tahoma,Arial,sans-serif}
body{padding:14px 12px 28px}
h1{font-size:19px;margin:2px 0 4px;text-align:center}
.up{text-align:center;color:var(--mut);font-size:13px;margin-bottom:12px}
.tot{background:var(--ink);color:#fff;border-radius:16px;padding:14px;text-align:center;margin-bottom:12px}
.tot small{display:block;opacity:.8;font-size:14px}
.tot b{font-size:28px;color:var(--or)}
#q{width:100%;font-size:17px;padding:12px 14px;border:2px solid var(--or);border-radius:14px;background:var(--or2);color:var(--ink);margin-bottom:12px;outline:none}
.card{background:var(--card);border-radius:14px;padding:6px 12px;box-shadow:0 1px 3px rgba(0,0,0,.08)}
.r{display:flex;justify-content:space-between;gap:8px;padding:10px 0;border-top:1px solid #EFE7DA;font-size:15px}
.r:first-child{border-top:0}
.r b{white-space:nowrap}
.neg{color:#C0392B}
.note{color:var(--mut);font-size:13px;text-align:center;margin-top:10px}
.cnt{color:var(--mut);font-size:13px;margin:0 2px 8px}
</style></head><body>
<h1 id="t">💰 الأرصدة</h1><div class="up" id="up"></div>
<div class="tot"><small id="tl">الإجمالي</small><b id="tot">—</b> <small>جنيه</small></div>
<input id="q" type="search" placeholder="🔎 اكتب جزء من الاسم..." autocomplete="off">
<div class="cnt" id="cnt"></div>
<div class="card"><div id="it"></div></div>
<div class="note" id="more"></div>
<script>
const tg=window.Telegram&&window.Telegram.WebApp; if(tg){tg.ready();tg.expand();}
const f=n=>Number(n).toLocaleString('en-US');
const norm=s=>String(s).replace(/[أإآ]/g,'ا').replace(/ة/g,'ه').replace(/ى/g,'ي').replace(/_/g,' ').toLowerCase().trim();
let D={};
try{
  const P=new URLSearchParams(location.search);
  let p=(P.get('d')||'').replace(/-/g,'+').replace(/_/g,'/');
  p+='='.repeat((4-p.length%4)%4);
  const bin=Uint8Array.from(atob(p),c=>c.charCodeAt(0));
  D=JSON.parse(P.get('z')==='1'?pako.inflateRaw(bin,{to:'string'}):new TextDecoder().decode(bin));
}catch(e){}
document.getElementById('t').textContent='💰 '+(D.t||'الأرصدة');
document.getElementById('up').textContent=D.u?('آخر تحديث: '+D.u):'';
document.getElementById('tl').textContent='الإجمالي'+(D.s?(' ('+D.s+')'):'');
document.getElementById('tot').textContent=D.total!==undefined?f(D.total):'—';
const items=D.it||[];
const box=document.getElementById('it'), cnt=document.getElementById('cnt');
function draw(){
  const toks=norm(document.getElementById('q').value).split(/\s+/).filter(Boolean);
  const list=items.filter(x=>{const n=norm(x[0]);return toks.every(t=>n.includes(t));});
  box.innerHTML='';
  list.forEach(x=>{const r=document.createElement('div');r.className='r';r.innerHTML='<span></span><b></b>';
    r.children[0].textContent=String(x[0]).replace(/_/g,' ');r.children[1].textContent=f(x[1]);
    if(x[1]<0)r.children[1].className='neg';box.append(r);});
  if(!list.length)box.textContent='مفيش نتيجة';
  cnt.textContent=list.length+' من '+items.length;
}
document.getElementById('q').addEventListener('input',draw);
draw();
if(D.more>0)document.getElementById('more').textContent='ظاهر أكبر '+items.length+' رصيد بس. فيه '+D.more+' كمان، دوّر عليهم من «عرض عادي» في البوت.';
</script></body></html>
'''
BAL_OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "miniapp", "balances.html")


def bal_base():
    return reports_base().rsplit("/", 1)[0] + "/balances.html"


def bal_data(cat, n):
    items, side = bal_items(cat)
    return {"k": "bal", "u": _cairo_now().strftime("%Y-%m-%d %H:%M"), "t": "أرصدة " + BAL_NAME.get(cat, cat), "s": side,
            "total": int(round(sum(v for _, v in items))), "more": max(0, len(items) - n),
            "it": [[k.replace("_", " "), int(round(v))] for k, v in items[:n]]}


def pack_bal_url(cat):
    """رابط فورم الأرصدة ببيانات طازة. لو طويل بيقلل عدد البنود لحد ما يدخل."""
    url = ""
    for n in (400, 250, 150, 100, 70, 50, 30, 15):
        raw = json.dumps(bal_data(cat, n), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        plain = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
        co = zlib.compressobj(9, zlib.DEFLATED, -15)
        zipped = base64.urlsafe_b64encode(co.compress(raw) + co.flush()).decode("ascii").rstrip("=")
        url = bal_base() + "?" + (urlencode({"d": plain}) if len(plain) <= 1500 else urlencode({"d": zipped, "z": 1}))
        if len(url) <= REPORT_URL_LIMIT:
            break
    return url


async def sr_cb(update, context):
    await ensure_roles()
    q = update.callback_query
    if q.from_user.id not in ADMINS:
        await q.answer("للمحاسبين بس", show_alert=True)
        return
    await q.answer()
    p = q.data.split(":")
    off = InlineKeyboardMarkup([[InlineKeyboardButton("إنهاء البحث", callback_data="sr:off")]])
    if p[1] == "txt":
        context.user_data["srch_wait"] = True
        context.user_data["srch_mode"] = "txt"
        await q.message.reply_text("🔎 اكتب أي حاجة تدوّر عليها (اسم عميل، رقم وصل، مبلغ، كلمة من البيان...):", reply_markup=off)
    elif p[1] == "bal":
        btns = [InlineKeyboardButton(n, callback_data=f"sr:c:{k}") for k, n in BAL_CATS]
        await q.message.reply_text("💰 عايز أرصدة إيه؟", reply_markup=InlineKeyboardMarkup(
            pairs(btns) + [[InlineKeyboardButton("إنهاء البحث", callback_data="sr:off")]]))
    elif p[1] == "c":
        cat = p[2]
        context.user_data["srch_wait"] = True
        context.user_data["srch_mode"] = "bal:" + cat
        if cat == "any":
            await q.message.reply_text("💰 اكتب جزء من اسم الحساب (مورد، عميل، خزنة، موظف، بند...):", reply_markup=off)
        elif MINIAPP_URL:
            try:
                url = await run(pack_bal_url, cat)
                await q.message.reply_text(f"✅ أرصدة {BAL_NAME[cat]} جاهزة بآخر الأرقام 👇", reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📊 افتح الفورم", web_app=WebAppInfo(url=url))],
                    [InlineKeyboardButton("📋 عرض عادي", callback_data=f"sr:n:{cat}")],
                    [InlineKeyboardButton("إنهاء البحث", callback_data="sr:off")]]))
            except Exception as e:
                logging.warning("balances form failed: %s", e)
                res = await run(bal_view, cat)
                await q.message.reply_text(res + "\n\nلو عايز تدوّر على اسم معين اكتبه دلوقتي.", reply_markup=off)
        else:
            res = await run(bal_view, cat)
            await q.message.reply_text(res + "\n\nلو عايز تدوّر على اسم معين اكتبه دلوقتي.", reply_markup=off)
    elif p[1] == "n":
        cat = p[2]
        context.user_data["srch_wait"] = True
        context.user_data["srch_mode"] = "bal:" + cat
        res = await run(bal_view, cat)
        await q.message.reply_text(res + "\n\nلو عايز تدوّر على اسم معين اكتبه دلوقتي.", reply_markup=off)


def main():
    ensure_ids_tab()
    ensure_rc_tabs()
    ensure_pending_cols()
    app = (Application.builder().token(TOKEN).post_init(post_init)
           .connect_timeout(30).read_timeout(30).write_timeout(30).pool_timeout(30)
           .get_updates_connect_timeout(30).get_updates_read_timeout(30)
           .get_updates_write_timeout(30).get_updates_pool_timeout(30).build())
    app.add_error_handler(on_error)
    app.add_handler(TypeHandler(Update, act_pre), group=-10)
    app.add_handler(CallbackQueryHandler(resend_cb, pattern=r"^rs:\d+$"), group=-4)
    text = filters.TEXT & ~filters.COMMAND & ~filters.Regex(START_RE)
    img = filters.PHOTO | filters.Document.IMAGE
    form_states = {
        F_VIEW: [CallbackQueryHandler(form_cb, pattern=r"^f:"), MessageHandler(text, form_text_in),
                 MessageHandler(img, form_photo_in)],
        F_TEXT: [CallbackQueryHandler(form_cb, pattern=r"^f:"), MessageHandler(text, form_text_in),
                 MessageHandler(img, form_photo_in)],
    }
    # الرفض بسبب + نص المدير (سبب / ملاحظة): قبل أي handler تاني
    app.add_handler(CallbackQueryHandler(rej_ask, pattern=REJ_PATTERN), group=-1)
    app.add_handler(CallbackQueryHandler(rej_cb, pattern=r"^rj:(skip|cancel)$"), group=-1)
    app.add_handler(MessageHandler(text & filters.User(OWNER_ID), owner_text_hook), group=-1)
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^" + CLOSE_BTN + "$"), close_btn), group=-3)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, rw_text), group=-6)
    app.add_handler(CommandHandler("editentry", ed_cmd), group=-2)
    app.add_handler(CallbackQueryHandler(ed_cb, pattern=r"^(menu:edit|ed:(?!new$)\w+)$"), group=-2)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, ed_text), group=-2)
    app.add_handler(CommandHandler("broadcast", bc_cmd), group=-1)
    app.add_handler(CallbackQueryHandler(bc_cb, pattern=r"^bc:"), group=-1)
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, mini_data), group=-1)
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^(" + "|".join(ALL_ROLE_BTNS) + ")$"), role_form_btn), group=-5)
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^" + re.escape(TZ_INV_BTN) + "$"), tz_inv_open), group=-5)
    app.add_handler(CallbackQueryHandler(rv_cb, pattern=r"^rv:"))
    app.add_handler(CallbackQueryHandler(mode_cb, pattern=r"^md:[nf]$"))
    app.add_handler(CallbackQueryHandler(mini_skip, pattern=r"^mp:skip$"), group=-1)
    app.add_handler(MessageHandler(_MiniWaiting(), mini_photo), group=-1)
    app.add_handler(CommandHandler("buildform", buildform_cmd))
    entries = [
        CommandHandler("start", start),
        MessageHandler(filters.Regex(START_RE), start),
        CommandHandler(["pay", "new"], start_pay),
        CommandHandler("supplier", sup_start),
        CommandHandler("sale", sale_start),
        CommandHandler("custody_return", cret_start),
        CommandHandler("advance_return", aret_start),
        CommandHandler("transfer", xfer_start),
        CommandHandler("expense", exp_start),
        CommandHandler("income", inc_start),
        CommandHandler("custody", cus_start),
        CommandHandler("advance", adv_start),
        CommandHandler("buy", buy_start),
        CommandHandler("newentry", gen_start),
        CallbackQueryHandler(gen_start, pattern=r"^(menu:gen|ed:new)$"),
        CallbackQueryHandler(sale_start, pattern=r"^menu:sale$"),
        CallbackQueryHandler(cret_start, pattern=r"^menu:cret$"),
        CallbackQueryHandler(aret_start, pattern=r"^menu:aret$"),
        CallbackQueryHandler(xfer_start, pattern=r"^menu:xfer$"),
        CallbackQueryHandler(exp_start, pattern=r"^menu:exp$"),
        CallbackQueryHandler(inc_start, pattern=r"^menu:inc$"),
        CallbackQueryHandler(cus_start, pattern=r"^menu:cus$"),
        CallbackQueryHandler(adv_start, pattern=r"^menu:adv$"),
        CallbackQueryHandler(start_pay, pattern=r"^menu:pay$"),
        CallbackQueryHandler(sup_start, pattern=r"^menu:sup$"),
        CallbackQueryHandler(buy_start, pattern=r"^menu:buy$"),
        CallbackQueryHandler(start_pay, pattern=r"^hm:pay$"),
        CallbackQueryHandler(rc_start, pattern=r"^hm:cus$"),
        MessageHandler(filters.PHOTO & filters.CaptionRegex(r"^\s*عميل"), photo_start),
        MessageHandler(_FormPhoto(False), pay_form_photo),
        CallbackQueryHandler(mini_stl_entry, pattern=r"^mp:stl$"),
        CallbackQueryHandler(tp_pick, pattern=r"^tp:[^:]+:[a-z]+$"),
        CallbackQueryHandler(cx_pick, pattern=r"^cq:[^:]+:[a-z]+$"),
    ]
    conv = ConversationHandler(
        entry_points=entries + [CallbackQueryHandler(owner_confirm, pattern=r"^ok:")],
        states={
            **form_states,
            SELLER: [CallbackQueryHandler(pick_seller, pattern=r"^s:")],
            SEARCH: [MessageHandler(text, search_client)],
            PTYPE: [CallbackQueryHandler(pick_type, pattern=r"^t:")],
            AMOUNT: [MessageHandler(text, got_amount)],
            KIND: [CallbackQueryHandler(pick_kind, pattern=r"^k:")],
            METHOD: [CallbackQueryHandler(pick_method, pattern=r"^m:")],
            OTHER_BANK: [MessageHandler(text, got_other_bank)],
            DATE: [CallbackQueryHandler(got_date, pattern=r"^d:"), MessageHandler(text, got_date)],
            NOTES: [CallbackQueryHandler(got_notes, pattern=r"^n:"), MessageHandler(text, got_notes)],
            CONFIRM: [
                CallbackQueryHandler(send_request, pattern=r"^go$"),
                CallbackQueryHandler(cancel, pattern=r"^cancel$"),
            ],
            PROJECT: [MessageHandler(text, got_project), CallbackQueryHandler(owner_back, pattern=r"^ob:")],
            ACCOUNTANT: [CallbackQueryHandler(pick_accountant, pattern=r"^u:"), CallbackQueryHandler(owner_back, pattern=r"^ob:")],
            S_SUP: [CallbackQueryHandler(sup_pick, pattern=r"^ss:")],
            S_TYPE: [CallbackQueryHandler(sup_type, pattern=r"^st:")],
            S_CTYPE: [CallbackQueryHandler(cus_type, pattern=r"^ct:")],
            S_EMP: [MessageHandler(text, emp_search), CallbackQueryHandler(emp_pick, pattern=r"^se:")],
            S_EL1: [CallbackQueryHandler(exp_l1, pattern=r"^xa:")],
            S_EL2: [CallbackQueryHandler(exp_l2, pattern=r"^xb:")],
            S_EL3: [CallbackQueryHandler(exp_l3, pattern=r"^xc:")],
            S_NOTE: [CallbackQueryHandler(exp_note, pattern=r"^xn:"), MessageHandler(text, exp_note)],
            S_SALETYPE: [CallbackQueryHandler(sale_type, pattern=r"^sy:")],
            S_STL: [CallbackQueryHandler(stl_pick, pattern=r"^sx:")],
            S_SELLER: [CallbackQueryHandler(sale_seller, pattern=r"^sl:")],
            S_CLIENT: [MessageHandler(text, sale_client)],
            S_PROJECT: [MessageHandler(text, sale_project)],
            S_AMOUNT: [MessageHandler(text, sup_amount)],
            S_KIND: [CallbackQueryHandler(sup_kind, pattern=r"^sk:")],
            S_METHOD: [CallbackQueryHandler(sup_method, pattern=r"^sm:")],
            S_OTHER: [MessageHandler(text, sup_other)],
            S_DATE: [CallbackQueryHandler(sup_date, pattern=r"^sd:"), MessageHandler(text, sup_date)],
            S_CONFIRM: [
                CallbackQueryHandler(sup_confirm, pattern=r"^sgo$"),
                CallbackQueryHandler(sup_cancel, pattern=r"^scancel$"),
            ],
            ATTACH: [MessageHandler(filters.PHOTO | filters.Document.IMAGE, got_attach),
                     CallbackQueryHandler(got_attach, pattern=r"^at:"), MessageHandler(text, got_attach)],
            P_VOUCH: [MessageHandler(text, got_pvoucher)],
            S_ATT: [MessageHandler(filters.PHOTO | filters.Document.IMAGE, s_att),
                    CallbackQueryHandler(s_att, pattern=r"^at:"), MessageHandler(text, s_att)],
            R_ATT: [MessageHandler(filters.PHOTO | filters.Document.IMAGE, rc_att),
                    CallbackQueryHandler(rc_att, pattern=r"^at:"), MessageHandler(text, rc_att)],
            S_VOUCH: [MessageHandler(text, sup_voucher)],
            R_DATE: [CallbackQueryHandler(rc_date, pattern=r"^rc:today$"), MessageHandler(text, rc_date)],
            R_BAYAN: [MessageHandler(text, rc_bayan)],
            R_AMOUNT: [MessageHandler(text, rc_amount)],
            R_NOTE: [CallbackQueryHandler(rc_note, pattern=r"^rc:skip$"), MessageHandler(text, rc_note)],
            R_CONFIRM: [
                CallbackQueryHandler(rc_confirm, pattern=r"^rc:go$"),
                CallbackQueryHandler(rc_cancel, pattern=r"^rc:cancel$"),
            ],
        },
        fallbacks=entries + [
            CommandHandler("cancel", cancel),
            CallbackQueryHandler(go_back, pattern=r"^back$"),
            CallbackQueryHandler(sgo_back, pattern=r"^sback$"),
            CallbackQueryHandler(rc_back, pattern=r"^rcback$"),
        ],
    )
    cash_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(cm_start, pattern=r"^cm:(exp|inc)$"),
                      CallbackQueryHandler(cash_form_open, pattern=r"^cm:form$"),
                      CallbackQueryHandler(tz_search_start, pattern=r"^tz:search$"),
                      CallbackQueryHandler(tcount_start, pattern=r"^ti:count$"),
                      MessageHandler(filters.TEXT & filters.Regex("^" + re.escape(TZ_SEARCH_BTN) + "$"), tz_search_start),
                      MessageHandler(_FormPhoto(True), cash_form_photo)],
        states={
            **form_states,
            C_BAYAN: [MessageHandler(text, cm_bayan)],
            C_AMOUNT: [MessageHandler(text, cm_amount)],
            C_DATE: [CallbackQueryHandler(cm_date, pattern=r"^cm:today$"), MessageHandler(text, cm_date)],
            C_PARTY: [MessageHandler(text, cm_party)],
            C_VOUCH: [MessageHandler(text, cm_voucher)],
            C_ATT: [MessageHandler(filters.PHOTO | filters.Document.IMAGE, cm_att),
                    CallbackQueryHandler(cm_att, pattern=r"^at:"), MessageHandler(text, cm_att)],
            C_CONFIRM: [CallbackQueryHandler(cm_confirm, pattern=r"^cm:go$")],
        },
        fallbacks=[CallbackQueryHandler(cm_back, pattern=r"^cm:back$"),
                   CallbackQueryHandler(cm_cancel, pattern=r"^cm:cancel$"), CommandHandler("cancel", cm_cancel),
                   CommandHandler("start", start), MessageHandler(filters.Regex(START_RE), start)],
    )
    app.add_handler(cash_conv)
    tr_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(tr_start, pattern=r"^tr:go$"),
                      MessageHandler(_TrPhoto(), tr_photo_start)],
        states={
            T_BAYAN: [MessageHandler(text, tr_bayan)],
            T_ATT: [MessageHandler(filters.PHOTO | filters.Document.IMAGE, tr_att), MessageHandler(text, tr_att)],
            T_CONFIRM: [CallbackQueryHandler(tr_confirm, pattern=r"^tc:go$")],
        },
        fallbacks=[CallbackQueryHandler(tr_back, pattern=r"^tr:back$"),
                   CallbackQueryHandler(tr_cancel, pattern=r"^tr:cancel$"), CommandHandler("cancel", tr_cancel),
                   CommandHandler("start", start), MessageHandler(filters.Regex(START_RE), start)],
    )
    app.add_handler(tr_conv)
    app.add_handler(CallbackQueryHandler(ti_cb, pattern=r"^ti:(?!count)"))
    app.add_handler(CallbackQueryHandler(cash_owner_menu, pattern=r"^menu:cash$"))
    app.add_handler(CallbackQueryHandler(go_home_cb, pattern=r"^gh:menu$"))
    app.add_handler(CallbackQueryHandler(search_open, pattern=r"^menu:search$"))
    app.add_handler(CallbackQueryHandler(search_off, pattern=r"^sr:off$"))
    app.add_handler(CallbackQueryHandler(sr_cb, pattern=r"^sr:(txt|bal|c:\w+|n:\w+)$"))
    app.add_handler(CallbackQueryHandler(act_cb, pattern=r"^as:"))
    app.add_handler(CallbackQueryHandler(tabdel_no, pattern=r"^tabdel:x:n$"))
    app.add_handler(CallbackQueryHandler(tabdel_cb, pattern=r"^tabdel:"))
    app.add_handler(CommandHandler("actas", act_list))
    app.add_handler(CommandHandler("me", act_off_cmd))
    app.add_handler(CommandHandler("azkar", azkar_cmd))
    app.add_handler(CommandHandler("custodytabs", custodytabs_cmd))
    app.add_handler(CommandHandler("cleantabs", cleantabs_cmd))
    app.add_handler(CommandHandler("search", search_cmd))
    app.add_handler(CommandHandler("treasury", treasury_cmd))
    app.add_handler(CallbackQueryHandler(ca_cb, pattern=r"^ca:ap$"))
    app.add_handler(CallbackQueryHandler(tq_reject, pattern=r"^tq:[^:]+:r$"))
    app.add_handler(CallbackQueryHandler(tq_approve, pattern=r"^tq:[^:]+:s$"))
    app.add_handler(conv)
    app.add_handler(CommandHandler("gltest", gl_test))
    app.add_handler(CommandHandler("cleardata", clear_cmd))
    app.add_handler(CallbackQueryHandler(cd_cb, pattern=r"^cd:"))
    app.add_handler(CallbackQueryHandler(gm_cb, pattern=r"^gm:"))
    app.add_handler(CommandHandler("sheetreports", reports_sheet_cmd))
    app.add_handler(CommandHandler("resetinbox", reset_inbox))
    app.add_handler(CallbackQueryHandler(cx_cb, pattern=r"^cx:"))
    app.add_handler(CommandHandler("myid", my_id))
    app.add_handler(CommandHandler("whoami", whoami))
    app.add_handler(CallbackQueryHandler(inbox_cb, pattern=r"^ib:"))
    app.add_handler(CallbackQueryHandler(rq_cb, pattern=r"^rq:"))
    app.add_handler(CallbackQueryHandler(report_cb, pattern=r"^rp:"))
    app.add_handler(CallbackQueryHandler(open_summary, pattern=r"^menu:sum$"))
    app.add_handler(CallbackQueryHandler(sync_cb, pattern=r"^sy:"))
    app.add_handler(CallbackQueryHandler(rw_cb, pattern=r"^(pz|sb):"))
    app.add_handler(CallbackQueryHandler(rebuild_cb, pattern=r"^rb:"))
    app.add_handler(CallbackQueryHandler(cash_close, pattern=r"^cm:close$"))
    app.add_handler(CommandHandler("rebuildtabs", rebuild_cmd))
    app.add_handler(CallbackQueryHandler(owner_reject, pattern=r"^no:"))
    app.add_handler(CallbackQueryHandler(mn_cb, pattern=r"^mn:"))
    app.add_handler(CallbackQueryHandler(pd_cb, pattern=r"^pd:"))
    app.add_handler(CallbackQueryHandler(ceo_cb, pattern=r"^ceo:"))
    app.run_polling()


if __name__ == "__main__":
    main()
