"""Short template wording for the draft auditor's report (report_draft.py).

A starting point the engagement team edits in Word, not finished wording: each section is a
heading and a few plain sentences. {entity}, {end}, {framework}, {note} and the like are
filled in from the engagement.
"""

REPORT = {
    "en": {
        "title": "Independent auditor's report",
        "to": "To {addressee}",
        "addressee": "the shareholders of {entity}",
        "opinion_heading": {"unmodified": "Opinion", "qualified": "Qualified opinion",
                            "adverse": "Adverse opinion", "disclaimer": "Disclaimer of opinion"},
        "scope": "We were engaged to audit the financial statements of {entity} for the year "
                 "ended {end}: the statement of financial position, the statements of profit "
                 "or loss, changes in equity and cash flows, and the notes.",
        "opinion": {
            "unmodified": "In our opinion, the financial statements give a true and fair view "
                          "of the company's position at {end} and of its results and cash "
                          "flows for the year, in line with {framework}.",
            "qualified": "In our opinion, apart from the effect of the matter set out below, the "
                         "financial statements give a true and fair view of the company's "
                         "position at {end} and of its results and cash flows for the year, in "
                         "line with {framework}.",
            "adverse": "In our opinion, because of the significance of the matter set out "
                       "below, the financial statements do not give a true and fair view of the "
                       "company's position at {end} or of its results and cash flows for the "
                       "year, in line with {framework}.",
            "disclaimer": "We do not express an opinion on the financial statements. Because of "
                          "the significance of the matter set out below, we could not obtain "
                          "enough appropriate evidence to form one.",
        },
        "basis_heading": {"unmodified": "Basis for opinion",
                          "qualified": "Basis for qualified opinion",
                          "adverse": "Basis for adverse opinion",
                          "disclaimer": "Basis for disclaimer of opinion"},
        "basis_placeholder": "[Describe the matter and, where you can, its effect in figures.]",
        "basis": "We audited in line with International Standards on Auditing and are "
                 "independent of the company under the ethical rules that apply to us. We "
                 "believe our evidence is sufficient and appropriate for our opinion.",
        "gc_heading": "Material uncertainty related to going concern",
        "gc": "We draw attention to note [X], which describes the following: {note}. This "
              "points to a material uncertainty about the company's ability to continue as a "
              "going concern. Our opinion is not modified for this matter.",
        "emphasis_heading": "Emphasis of matter",
        "emphasis_end": "Our opinion is not modified for this matter.",
        "kam_heading": "Key audit matters",
        "kam_intro": "These are the matters we judged most significant in this year's audit. "
                     "We dealt with them as part of the audit as a whole and give no separate "
                     "opinion on them.",
        "kam_title": "[Name the key audit matter]",
        "kam_why": "Why it mattered: {text}",
        "kam_how": "How we dealt with it: {text}",
        "other_heading": "Other matter",
        "mgmt_heading": "Responsibilities of management and those charged with governance",
        "mgmt": "Management is responsible for preparing the financial statements in line with "
                "{framework}, for the internal control needed to keep them free of material "
                "misstatement, and for assessing whether the company is a going concern. Those "
                "charged with governance oversee financial reporting.",
        "auditor_heading": "Auditor's responsibilities",
        "auditor": "We aim for reasonable assurance, which is high but not absolute, that the "
                   "financial statements as a whole are free of material misstatement from "
                   "fraud or error, and to report our opinion. [Add or refer to the full "
                   "description of the auditor's responsibilities.]",
        "auditor_disclaimer": "Our job is to audit the financial statements under International "
                              "Standards on Auditing and report on them. Because of the matter "
                              "set out above, we could not obtain enough appropriate evidence "
                              "to form an opinion. We are independent of the company under the "
                              "ethical rules that apply to us.",
        "partner": "Engagement partner: {partner}",
        "firm": "[Audit firm]",
        "city": "[City]",
        "date": "[Report date]",
        "frameworks": {"IFRS": "IFRS Accounting Standards",
                       "IFRS for SMEs": "the IFRS for SMEs Accounting Standard",
                       "US GAAP": "US generally accepted accounting principles"},
        "framework_other": "[the applicable financial reporting framework]",
    },
}

REPORT["ar"] = {
    "title": "تقرير المراجع المستقل",
    "to": "إلى {addressee}",
    "addressee": "السادة مساهمي {entity}",
    "opinion_heading": {"unmodified": "الرأي", "qualified": "الرأي المتحفظ",
                        "adverse": "الرأي المعارض", "disclaimer": "الامتناع عن إبداء الرأي"},
    "scope": "كُلِّفنا بمراجعة القوائم المالية لـ {entity} للسنة المنتهية في {end}: قائمة المركز "
             "المالي، وقوائم الربح أو الخسارة والتغيرات في حقوق الملكية والتدفقات النقدية، "
             "والإيضاحات.",
    "opinion": {
        "unmodified": "في رأينا، تعرض القوائم المالية بصورة عادلة مركز الشركة في {end} "
                      "ونتائجها وتدفقاتها النقدية للسنة، وفق {framework}.",
        "qualified": "في رأينا، وباستثناء أثر الأمر المبين أدناه، تعرض القوائم المالية بصورة "
                     "عادلة مركز الشركة في {end} ونتائجها وتدفقاتها النقدية للسنة، وفق "
                     "{framework}.",
        "adverse": "في رأينا، ونظراً لأهمية الأمر المبين أدناه، لا تعرض القوائم المالية بصورة "
                   "عادلة مركز الشركة في {end} ولا نتائجها وتدفقاتها النقدية للسنة، وفق "
                   "{framework}.",
        "disclaimer": "لا نبدي رأياً في القوائم المالية. ونظراً لأهمية الأمر المبين أدناه، لم "
                      "نتمكن من الحصول على أدلة كافية ومناسبة لتكوين رأي.",
    },
    "basis_heading": {"unmodified": "أساس الرأي", "qualified": "أساس الرأي المتحفظ",
                      "adverse": "أساس الرأي المعارض",
                      "disclaimer": "أساس الامتناع عن إبداء الرأي"},
    "basis_placeholder": "[صف الأمر، واذكر أثره بالأرقام متى أمكن.]",
    "basis": "أجرينا المراجعة وفقاً للمعايير الدولية للمراجعة، ونحن مستقلون عن الشركة وفقاً "
             "لقواعد السلوك المهني المنطبقة علينا. ونرى أن الأدلة التي حصلنا عليها كافية "
             "ومناسبة لرأينا.",
    "gc_heading": "عدم تأكد جوهري يتعلق بالاستمرارية",
    "gc": "نلفت الانتباه إلى الإيضاح [X] الذي يبين ما يلي: {note}. ويشير ذلك إلى عدم تأكد "
          "جوهري بشأن قدرة الشركة على الاستمرار. ولم يُعدَّل رأينا بشأن هذا الأمر.",
    "emphasis_heading": "لفت انتباه",
    "emphasis_end": "ولم يُعدَّل رأينا بشأن هذا الأمر.",
    "kam_heading": "أمور المراجعة الرئيسية",
    "kam_intro": "هذه هي الأمور التي رأينا أنها الأكثر أهمية في مراجعة هذه السنة. وقد تناولناها "
                 "ضمن المراجعة ككل، ولا نبدي رأياً منفصلاً فيها.",
    "kam_title": "[اذكر أمر المراجعة الرئيسي]",
    "kam_why": "سبب أهميته: {text}",
    "kam_how": "كيف تناولناه: {text}",
    "other_heading": "أمر آخر",
    "mgmt_heading": "مسؤوليات الإدارة والمكلفين بالحوكمة",
    "mgmt": "الإدارة مسؤولة عن إعداد القوائم المالية وفق {framework}، وعن الرقابة الداخلية "
            "اللازمة لخلوها من التحريف الجوهري، وعن تقييم قدرة الشركة على الاستمرار. ويشرف "
            "المكلفون بالحوكمة على التقرير المالي.",
    "auditor_heading": "مسؤوليات المراجع",
    "auditor": "نسعى إلى الحصول على تأكيد معقول، وهو تأكيد عالٍ وليس مطلقاً، بأن القوائم المالية "
               "ككل خالية من التحريف الجوهري الناتج عن غش أو خطأ، وإلى إبداء رأينا. [أضف الوصف "
               "الكامل لمسؤوليات المراجع أو أشر إليه.]",
    "auditor_disclaimer": "مهمتنا مراجعة القوائم المالية وفقاً للمعايير الدولية للمراجعة "
                          "والتقرير عنها. ونظراً للأمر المبين أعلاه، لم نتمكن من الحصول على "
                          "أدلة كافية ومناسبة لتكوين رأي. ونحن مستقلون عن الشركة وفقاً لقواعد "
                          "السلوك المهني المنطبقة علينا.",
    "partner": "الشريك المسؤول عن الارتباط: {partner}",
    "firm": "[اسم مكتب المراجعة]",
    "city": "[المدينة]",
    "date": "[تاريخ التقرير]",
    "frameworks": {"IFRS": "المعايير الدولية للتقرير المالي",
                   "IFRS for SMEs": "المعيار الدولي للتقرير المالي للمنشآت الصغيرة والمتوسطة",
                   "US GAAP": "المبادئ المحاسبية المقبولة عموماً في الولايات المتحدة"},
    "framework_other": "[إطار التقرير المالي المنطبق]",
}
