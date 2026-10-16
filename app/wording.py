"""Template wording for the draft representation letter (completion.py).

Short plain sentences the team edits in Word before sending. {entity}, {end}, {framework}
and {firm} are filled in from the engagement.
"""

LETTER = {
    "en": {
        "title": "Representation letter",
        "to": "To: {firm}",
        "date": "Date: {date}",
        "intro": "We give you this letter for your audit of the financial statements of "
                 "{entity} for the year ended {end}, prepared under {framework}.",
        "confirm": "After making the inquiries we thought necessary, we confirm the following "
                   "to the best of our knowledge.",
        "fs": "The financial statements",
        "fs_items": [
            "We are responsible for the financial statements and have prepared them under "
            "{framework}, as agreed in the engagement letter.",
            "Our accounting estimates, and the assumptions behind them, are reasonable.",
            "All related parties and related party transactions are recorded and disclosed.",
            "Every event after the period end that needs an adjustment or a disclosure has "
            "been dealt with.",
            "We have told you about all known or possible litigation and claims.",
            "Preparing the statements on a going concern basis is appropriate, and we have "
            "told you everything we know that bears on it.",
        ],
        "uncorrected": "The uncorrected misstatements in the schedule to this letter are not "
                       "material, alone or together.",
        "events": "Events after the period end we have told you about:",
        "info": "Information we gave you",
        "info_items": [
            "We gave you all the records and information relevant to the statements, everything "
            "else you asked for, and free access to our staff.",
            "Every transaction is recorded in the books and reflected in the statements.",
            "We have told you how we assess the risk of fraud, and about any fraud or suspected "
            "fraud we know of.",
            "We have told you about any breach, or suspected breach, of laws and regulations "
            "we know of.",
        ],
        "close": "Yours faithfully,",
        "signatures": ["____________________\nChief Executive Officer",
                       "____________________\nChief Financial Officer"],
        "schedule": "Schedule: uncorrected misstatements",
        "schedule_line": "{ref} {description}: profit {profit}, net assets {net_assets}",
        "schedule_total": "Total: profit {profit}, net assets {net_assets}",
        "event_line": "{date}{description} ({kind}, {treatment})",
        "kinds": {"adjusting": "adjusting", "non_adjusting": "non-adjusting"},
        "treatments": {"pending": "to decide", "adjusted": "adjusted",
                       "disclosed": "disclosed", "not_needed": "no change needed"},
        "firm": "[Audit firm]",
        "no_date": "[Date, close to the date of the audit report]",
    },
    "ar": {
        "title": "خطاب الإفادات",
        "to": "إلى: {firm}",
        "date": "التاريخ: {date}",
        "intro": "نقدم لكم هذا الخطاب لأغراض مراجعتكم للقوائم المالية لـ {entity} للسنة "
                 "المنتهية في {end}، والمعدة وفق {framework}.",
        "confirm": "وبعد إجراء الاستفسارات التي رأيناها ضرورية، نؤكد لكم ما يلي على حد علمنا.",
        "fs": "القوائم المالية",
        "fs_items": [
            "نحن مسؤولون عن القوائم المالية، وقد أعددناها وفق {framework} كما هو متفق عليه "
            "في خطاب الارتباط.",
            "تقديراتنا المحاسبية والافتراضات التي بُنيت عليها معقولة.",
            "جميع الأطراف ذات العلاقة ومعاملاتها مسجلة ومفصح عنها.",
            "تمت معالجة كل حدث لاحق لنهاية الفترة يستلزم تعديلاً أو إفصاحاً.",
            "أبلغناكم بجميع الدعاوى والمطالبات القائمة أو المحتملة التي نعلم بها.",
            "إعداد القوائم على أساس الاستمرارية مناسب، وأبلغناكم بكل ما نعلمه مما يؤثر عليه.",
        ],
        "uncorrected": "التحريفات غير المصححة الواردة في الجدول المرفق بهذا الخطاب غير جوهرية، "
                       "منفردة أو مجتمعة.",
        "events": "الأحداث اللاحقة لنهاية الفترة التي أبلغناكم بها:",
        "info": "المعلومات التي قدمناها لكم",
        "info_items": [
            "قدمنا لكم جميع السجلات والمعلومات ذات الصلة بالقوائم، وكل ما طلبتموه إضافة إلى "
            "ذلك، وأتحنا لكم التواصل بحرية مع موظفينا.",
            "جميع المعاملات مسجلة في الدفاتر ومنعكسة في القوائم.",
            "أبلغناكم بكيفية تقييمنا لخطر الغش، وبأي غش أو اشتباه في غش نعلم به.",
            "أبلغناكم بأي مخالفة، أو اشتباه في مخالفة، للأنظمة واللوائح نعلم بها.",
        ],
        "close": "وتفضلوا بقبول فائق الاحترام،",
        "signatures": ["____________________\nالرئيس التنفيذي",
                       "____________________\nالمدير المالي"],
        "schedule": "جدول: التحريفات غير المصححة",
        "schedule_line": "{ref} {description}: الربح {profit}، صافي الأصول {net_assets}",
        "schedule_total": "الإجمالي: الربح {profit}، صافي الأصول {net_assets}",
        "event_line": "{date}{description} ({kind}، {treatment})",
        "kinds": {"adjusting": "يستوجب التعديل", "non_adjusting": "لا يستوجب التعديل"},
        "treatments": {"pending": "لم يُحسم بعد", "adjusted": "تم تعديله",
                       "disclosed": "تم الإفصاح عنه", "not_needed": "لا يلزم تغيير"},
        "firm": "[اسم مكتب المراجعة]",
        "no_date": "[التاريخ، قريباً من تاريخ تقرير المراجعة]",
    },
}
