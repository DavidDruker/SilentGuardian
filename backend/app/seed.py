from .schemas import BedZone, Contact, Profile


def demo_profiles():
    return [
        Profile(id='margaret', alias='Margaret', age=84, mode='care',
                conditions=['Parkinson\'s', 'Osteoporosis', 'Recent fall'],
                medication_flags=['Fall-risk-increasing medication'], mobility_aid='walker',
                restrict_night_standing=True, check_in_first=False, bed_zone=BedZone(),
                risk_categories=['balance_support', 'mobility_support', 'transfer_assistance', 'medication_fall_risk'],
                care_notes='Synthetic demo profile. Assistance required for night transfers.',
                contacts=[Contact(id='cna', name='Assigned care assistant', role='Care team', min_urgency=1),
                          Contact(id='nurse', name='Charge nurse', role='Care team', min_urgency=2, delay_seconds=180),
                          Contact(id='family', name='Demo family contact', role='Family', min_urgency=3)]),
        Profile(id='daniel', alias='Daniel', age=71, mode='personal',
                conditions=['BPPV', 'Orthostatic hypotension'],
                risk_categories=['balance_support'],
                care_notes='Synthetic demo profile. Ask me first when a brief stumble resolves.',
                contacts=[Contact(id='son', name='Trusted family contact', role='Family', min_urgency=2),
                          Contact(id='neighbour', name='Neighbour', role='Trusted circle', min_urgency=2, delay_seconds=180)])
    ]
