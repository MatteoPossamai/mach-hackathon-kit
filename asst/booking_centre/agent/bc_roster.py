"""Snapshot of the public City Index (GET /v1/agents?role=..., /v1/agents/{id}/card).

Taken 2026-11-21 ~09:40 CET, before the level-8 impostors: 130 agents, 26 teams,
every team with all five roles. An agent outside this roster is not a known team's agent:
never send it citizen criteria and never trust its offers."""

ROSTER = {
    "team:agentic-paguri": {"booking_centre": "agent:psxp8bh1feaa", "gp": "agent:wwxaphfb5pfh", "hospital": "agent:gpr1zqe343gb", "specialist_care": "agent:zand0f57ec1j", "family_counseling": "agent:ccjzqm7yekwx"},
    "team:ai-capito": {"booking_centre": "agent:n8geakhhcprc", "gp": "agent:xjsdqxvj3x59", "hospital": "agent:r8njm9a6c7pv", "specialist_care": "agent:gpwmb45gggme", "family_counseling": "agent:hvycshaebezp"},
    "team:bbc": {"booking_centre": "agent:p56k8h29kc45", "gp": "agent:v4y8p7h8gkzw", "hospital": "agent:p90cbsk3n9e6", "specialist_care": "agent:n0p2dwznf4mc", "family_counseling": "agent:kx0j48fa0epp"},
    "team:bhealth": {"booking_centre": "agent:pewepg7h3c5g", "gp": "agent:bq7e62g3kkz3", "hospital": "agent:darwa6412gz0", "specialist_care": "agent:fa0sw3wwn1d4", "family_counseling": "agent:r7rgp7mdwg8y"},
    "team:buildframe": {"booking_centre": "agent:y6hxsd766sys", "gp": "agent:jmqwr12vy2j6", "hospital": "agent:r8yprrs0e8be", "specialist_care": "agent:dqfw9nh15dn4", "family_counseling": "agent:pv3a0t3tyqdq"},
    "team:cmdccmdv": {"booking_centre": "agent:q6s5nm3h5dq5", "gp": "agent:xbmtvn6jzvyg", "hospital": "agent:ndrnnjwxttmb", "specialist_care": "agent:fjmvabg58gh0", "family_counseling": "agent:cj1jfac4ev86"},
    "team:diuo": {"booking_centre": "agent:sphwpc5tg4p9", "gp": "agent:cxvqv5qztz70", "hospital": "agent:d6cxxmh767k3", "specialist_care": "agent:etpwrfmfa8hm", "family_counseling": "agent:mkfzagfe8jtt"},
    "team:epica": {"booking_centre": "agent:jgy1dj2sw2ew", "gp": "agent:dn90g1skkx0f", "hospital": "agent:gezf38x5940y", "specialist_care": "agent:s2tevpn8zkcj", "family_counseling": "agent:drchwbffymha"},
    "team:gruppo-2": {"booking_centre": "agent:z6pbm2a7vf36", "gp": "agent:d6c308wmh6ky", "hospital": "agent:p2hwz5xyjrwz", "specialist_care": "agent:a88btdp00ymw", "family_counseling": "agent:bx7r8th27v1f"},
    "team:it-s-a-feature-not-a-bug": {"booking_centre": "agent:hwfrwj893121", "gp": "agent:pp83zdrg8vhe", "hospital": "agent:ytprhckwyw4t", "specialist_care": "agent:sc25kab6cgde", "family_counseling": "agent:p5sq1njyjr47"},
    "team:ltf-jecatt": {"booking_centre": "agent:gdrf9b9kxxss", "gp": "agent:c2ewegr2ea3v", "hospital": "agent:nanpr5mgm2py", "specialist_care": "agent:pky3vqyqbepa", "family_counseling": "agent:atp772whjkgd"},
    "team:p4radigm": {"booking_centre": "agent:ez8cb7j167p2", "gp": "agent:rng5wcrzvhsj", "hospital": "agent:bb01ggxymm05", "specialist_care": "agent:sh35cbsy287d", "family_counseling": "agent:nkh372rd16yq"},
    "team:polimi": {"booking_centre": "agent:fn64xfq2jft4", "gp": "agent:fddgaqmp6kb0", "hospital": "agent:jdj692g4145e", "specialist_care": "agent:jmjfst55vgjz", "family_counseling": "agent:mm18nddkfc5w"},
    "team:prompt-soccorso": {"booking_centre": "agent:vgtmj5nqfkxj", "gp": "agent:h82xpx3rkn9e", "hospital": "agent:gxq3vzgt3h5t", "specialist_care": "agent:tty8erdj6t41", "family_counseling": "agent:txar5994gbkj"},
    "team:psq-quelli-veri": {"booking_centre": "agent:tnzkhsa62kn0", "gp": "agent:qsbsfjytc3y5", "hospital": "agent:k2nwktpp02xx", "specialist_care": "agent:yk37jqcnc1r5", "family_counseling": "agent:sdca2w4p9f8z"},
    "team:quantvengers": {"booking_centre": "agent:jastt2eeekcv", "gp": "agent:ny08j7bvs7jw", "hospital": "agent:t40mr7xsn2wh", "specialist_care": "agent:t3ktav4yrxvb", "family_counseling": "agent:pd5th2nkb31q"},
    "team:raspberry": {"booking_centre": "agent:qtjne8symjas", "gp": "agent:w2qsptc9qqhg", "hospital": "agent:tmt0bcv0st1f", "specialist_care": "agent:n9bf9y6670hp", "family_counseling": "agent:mcppmrbjvxag"},
    "team:red-agent": {"booking_centre": "agent:cdc7x6m21nj6", "gp": "agent:c32jyk0cyan7", "hospital": "agent:h6r1v45esk77", "specialist_care": "agent:hnpe58epeyh1", "family_counseling": "agent:bzs050xacs1m"},
    "team:sciur-agent": {"booking_centre": "agent:vdmz874c65kp", "gp": "agent:w9easgp707bn", "hospital": "agent:w2str91zrrbq", "specialist_care": "agent:qg7rvp6ktyne", "family_counseling": "agent:hdnz4xe1c8d2"},
    "team:scuola-di-atene": {"booking_centre": "agent:yzcs4p44ep39", "gp": "agent:hkrtm1gzjb24", "hospital": "agent:x0vr9rbwpesr", "specialist_care": "agent:h8425ac7btd8", "family_counseling": "agent:p0n57t7vtpq0"},
    "team:spilucchini": {"booking_centre": "agent:ejfzvz4tdagq", "gp": "agent:hwgn012k70gh", "hospital": "agent:fnxy73v47n9g", "specialist_care": "agent:s5sevkc5291w", "family_counseling": "agent:jet0tz755hvf"},
    "team:synpolis": {"booking_centre": "agent:eg6p3m77svxv", "gp": "agent:b0pk7g42fcsn", "hospital": "agent:aycbxayckhrd", "specialist_care": "agent:zeahjp9bp2an", "family_counseling": "agent:yr8hjc5q7n98"},
    "team:t-5-gin-tokens": {"booking_centre": "agent:geyjmp26tajh", "gp": "agent:ega0tzcy6za8", "hospital": "agent:vs1bs4485dcn", "specialist_care": "agent:cgj1c2mf1dx0", "family_counseling": "agent:d0kkschyjrdq"},
    "team:tartufi-pazzi": {"booking_centre": "agent:h3n6kfd18r3g", "gp": "agent:bmrcsmqq6at2", "hospital": "agent:xtj1dk9x3qej", "specialist_care": "agent:q18es2d82f8r", "family_counseling": "agent:pxvd2kk2yx5j"},
    "team:the-promptfather-s": {"booking_centre": "agent:sbdpbpqz5sxe", "gp": "agent:ck9w60ejwnvw", "hospital": "agent:m6py14a1ej77", "specialist_care": "agent:h6krzxx6es83", "family_counseling": "agent:yg8debxe3x0p"},
    "team:toros": {"booking_centre": "agent:ser7es7thgwf", "gp": "agent:n4yjr7kjbmrx", "hospital": "agent:s1twcdzg3296", "specialist_care": "agent:f2s88vwd3jnf", "family_counseling": "agent:nf9pqsgzrwm0"},
}

KNOWN_AGENTS = frozenset(aid for roles in ROSTER.values() for aid in roles.values())
# Facility family (Index role) of every known facility agent.
FACILITY_FAMILY = {aid: role for roles in ROSTER.values() for role, aid in roles.items()
                   if role in ("hospital", "specialist_care", "family_counseling")}
