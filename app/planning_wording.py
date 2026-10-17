"""Short template wording for the engagement letter and the planning memo (planning.py).

A starting point the team edits in Word, not finished wording: each section is a heading and a
few plain sentences. {entity}, {end}, {framework} and the like are filled in from the
engagement.
"""

LETTER = {
    "en": {
        "title": "Audit engagement letter",
        "to": "To {addressee}",
        "addressee": "the board of directors of {entity}",
        "date": "[Date]",
        "scope_heading": "Objective and scope",
        "scope": "You have asked us to audit the financial statements of {entity} for the year "
                 "ending {end}. We will audit them in line with International Standards on "
                 "Auditing. Our aim is to give an opinion on whether they give a true and fair "
                 "view in line with {framework}.",
        "ours_heading": "Our responsibilities",
        "ours": "We will plan and carry out the audit to obtain reasonable assurance that the "
                "financial statements are free of material misstatement. We test on a sample "
                "basis, so some misstatements may not be found. We will tell you in writing of "
                "any significant weaknesses in internal control we find.",
        "mgmt_heading": "Management's responsibilities",
        "mgmt": "Management is responsible for preparing the financial statements in line with "
                "{framework}, and for the internal control this needs. Management will give us "
                "the records and information we ask for, access to staff, and written "
                "representations at the end of the audit.",
        "report_heading": "Our report",
        "report_due": "We plan to issue our report by {due}.",
        "report_open": "We will agree the date of our report with you.",
        "report_modified": "If our opinion needs to be modified, we will discuss the reasons "
                           "with you first.",
        "other_heading": "Other services",
        "fee_heading": "Fees",
        "fee_fixed": "Our fee for the audit is {fee}, plus out-of-pocket expenses.",
        "fee_hourly": "Our fee is based on the time spent at our hourly rates. We estimate it "
                      "at {fee}.",
        "fee_none": "[Set out the fee and how it is billed.]",
        "agree_heading": "Agreement",
        "agree": "Please sign and return a copy of this letter to confirm that you agree to "
                 "these terms.",
        "firm": "[Audit firm]",
        "partner": "Engagement partner: {partner}",
        "city": "[City]",
        "accepted_heading": "Accepted on behalf of {entity}",
        "accepted_by": "Name: {name}",
        "accepted_on": "Date: {date}",
        "blank": "______________",
    },
    "ar": {
        "title": "خطاب ارتباط المراجعة",
        "to": "إلى {addressee}",
        "addressee": "مجلس إدارة {entity}",
        "date": "[التاريخ]",
        "scope_heading": "الهدف والنطاق",
        "scope": "طلبتم منا مراجعة القوائم المالية لـ {entity} عن السنة المنتهية في {end}. "
                 "سنراجعها وفقاً لمعايير المراجعة الدولية. هدفنا إبداء رأي فيما إذا كانت تعرض "
                 "بعدل وفقاً لـ {framework}.",
        "ours_heading": "مسؤولياتنا",
        "ours": "سنخطط للمراجعة وننفذها للحصول على تأكيد معقول بأن القوائم المالية خالية من "
                "التحريف الجوهري. نختبر على أساس العينات، لذا قد لا تُكتشف بعض التحريفات. "
                "سنبلغكم كتابةً بأي أوجه قصور مهمة في الرقابة الداخلية نكتشفها.",
        "mgmt_heading": "مسؤوليات الإدارة",
        "mgmt": "الإدارة مسؤولة عن إعداد القوائم المالية وفقاً لـ {framework}، وعن الرقابة "
                "الداخلية اللازمة لذلك. ستزودنا الإدارة بالسجلات والمعلومات التي نطلبها، "
                "وبإمكانية الوصول إلى الموظفين، وبإفادات مكتوبة في نهاية المراجعة.",
        "report_heading": "تقريرنا",
        "report_due": "نخطط لإصدار تقريرنا بحلول {due}.",
        "report_open": "سنتفق معكم على تاريخ تقريرنا.",
        "report_modified": "إذا احتاج رأينا إلى تعديل، فسنناقش الأسباب معكم أولاً.",
        "other_heading": "خدمات أخرى",
        "fee_heading": "الأتعاب",
        "fee_fixed": "أتعابنا عن المراجعة {fee}، بالإضافة إلى المصروفات الفعلية.",
        "fee_hourly": "تُحسب أتعابنا على أساس الوقت المستغرق بأسعارنا للساعة. نقدرها بمبلغ "
                      "{fee}.",
        "fee_none": "[اذكر الأتعاب وطريقة تحصيلها.]",
        "agree_heading": "الموافقة",
        "agree": "يرجى توقيع نسخة من هذا الخطاب وإعادتها تأكيداً لموافقتكم على هذه الشروط.",
        "firm": "[مكتب المراجعة]",
        "partner": "الشريك المسؤول: {partner}",
        "city": "[المدينة]",
        "accepted_heading": "تمت الموافقة نيابةً عن {entity}",
        "accepted_by": "الاسم: {name}",
        "accepted_on": "التاريخ: {date}",
        "blank": "______________",
    },
}

MEMO = {
    "en": {
        "title": "Audit planning memorandum",
        "entity": "Entity: {entity}",
        "period": "Year ending: {end}",
        "framework": "Framework: {framework}",
        "prepared": "Prepared by: {name}",
        "business_heading": "The entity and its business",
        "business_placeholder": "[Describe the business, its industry and any changes this "
                                "year.]",
        "acceptance_heading": "Acceptance",
        "accepted": "The engagement was accepted by {name} on {date}.",
        "declined": "The partner's decision was to decline the engagement.",
        "acceptance_open": "The acceptance decision is not recorded yet.",
        "letter_accepted": "The client accepted the engagement letter on {date}.",
        "letter_open": "The client has not yet accepted the engagement letter.",
        "materiality_heading": "Materiality",
        "overall": "Overall materiality: {amount}",
        "performance": "Performance materiality: {amount}",
        "trivial": "Clearly trivial threshold: {amount}",
        "basis": "Basis: {pct}% of {benchmark} ({amount}).",
        "materiality_open": "Materiality is not set yet.",
        "risks_heading": "Risks of material misstatement",
        "risk": "{ref} {title} ({level}, {area})",
        "risk_response": "Response: {text}",
        "risks_none": "No risks are on the register yet.",
        "risks_more": "The full register has {n} risks; the lower risks are in the file.",
        "approach_heading": "Audit approach",
        "approach": {
            "substantive": "We plan a substantive approach: tests of details and analytical "
                           "procedures, without relying on controls.",
            "combined": "We plan a combined approach: we will test the key controls and rely "
                        "on them, and reduce our substantive tests to match.",
        },
        "group": "The group has {n} components; {full} are in full scope.",
        "team_heading": "Team and budget",
        "member": "{name}, {role}",
        "team_none": "No one is on the team yet.",
        "budget": "Budgeted hours: {hours}",
        "budget_none": "No time budget is set yet.",
        "booked": "Hours booked on the schedule: {hours}",
        "timetable_heading": "Timetable",
        "report_due": "Report due by: {date}",
        "timetable_placeholder": "[Set out the key dates: fieldwork, clearance meeting and "
                                 "report.]",
        "other_heading": "Other matters",
        "levels": {"significant": "significant risk", "higher": "higher risk",
                   "lower": "lower risk"},
        "roles": {"preparer": "preparer", "reviewer": "reviewer", "partner": "partner",
                  "admin": "admin"},
        "benchmarks": {"pbt": "profit before tax", "revenue": "revenue",
                       "total_assets": "total assets", "equity": "equity",
                       "expenses": "total expenses"},
    },
    "ar": {
        "title": "مذكرة تخطيط المراجعة",
        "entity": "المنشأة: {entity}",
        "period": "السنة المنتهية في: {end}",
        "framework": "الإطار: {framework}",
        "prepared": "أعدها: {name}",
        "business_heading": "المنشأة ونشاطها",
        "business_placeholder": "[صف النشاط والقطاع وأي تغييرات خلال السنة.]",
        "acceptance_heading": "القبول",
        "accepted": "قبل {name} الارتباط بتاريخ {date}.",
        "declined": "قرر الشريك رفض الارتباط.",
        "acceptance_open": "لم يُسجل قرار القبول بعد.",
        "letter_accepted": "وافق العميل على خطاب الارتباط بتاريخ {date}.",
        "letter_open": "لم يوافق العميل على خطاب الارتباط بعد.",
        "materiality_heading": "الأهمية النسبية",
        "overall": "الأهمية النسبية الكلية: {amount}",
        "performance": "الأهمية النسبية للتنفيذ: {amount}",
        "trivial": "حد المبالغ الضئيلة بوضوح: {amount}",
        "basis": "الأساس: {pct}% من {benchmark} ({amount}).",
        "materiality_open": "لم تُحدد الأهمية النسبية بعد.",
        "risks_heading": "مخاطر التحريف الجوهري",
        "risk": "{ref} {title} ({level})",
        "risk_response": "الاستجابة: {text}",
        "risks_none": "لا توجد مخاطر في السجل بعد.",
        "risks_more": "يضم السجل الكامل {n} من المخاطر، والمخاطر الأقل موجودة في الملف.",
        "approach_heading": "منهج المراجعة",
        "approach": {
            "substantive": "نخطط لمنهج أساسي: اختبارات تفصيلية وإجراءات تحليلية، دون "
                           "الاعتماد على أدوات الرقابة.",
            "combined": "نخطط لمنهج مختلط: سنختبر أدوات الرقابة الرئيسية ونعتمد عليها، "
                        "ونخفض الاختبارات الأساسية بما يتناسب مع ذلك.",
        },
        "group": "تضم المجموعة {n} من المكونات، منها {full} بنطاق كامل.",
        "team_heading": "الفريق والموازنة",
        "member": "{name}، {role}",
        "team_none": "لا يوجد أحد في الفريق بعد.",
        "budget": "الساعات المقدرة: {hours}",
        "budget_none": "لم تُحدد موازنة للوقت بعد.",
        "booked": "الساعات المحجوزة في الجدول: {hours}",
        "timetable_heading": "الجدول الزمني",
        "report_due": "موعد التقرير: {date}",
        "timetable_placeholder": "[اذكر المواعيد الرئيسية: العمل الميداني واجتماع الختام "
                                 "والتقرير.]",
        "other_heading": "أمور أخرى",
        "levels": {"significant": "خطر مهم", "higher": "خطر أعلى", "lower": "خطر أقل"},
        "roles": {"preparer": "معد", "reviewer": "مراجع", "partner": "شريك", "admin": "مدير"},
        "benchmarks": {"pbt": "الربح قبل الضريبة", "revenue": "الإيرادات",
                       "total_assets": "إجمالي الأصول", "equity": "حقوق الملكية",
                       "expenses": "إجمالي المصروفات"},
    },
}
