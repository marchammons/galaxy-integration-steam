__version__ = "0.46"
__changelog__ = {
    "0.46": '''
    - Protential fixes for key error on achievements retrieval
    - Retrieve last_played time from protobuf instead of website scrapping
    ''',
    "0.45": '''
    - Quickfix for plugin crash while getting achievements
    - Restore mechanizm to get last_played time if game was launched via Steam
    ''',
    "0.44": '''
    - Achievements and GameTime are now pulled from protobufs instead of scrapping the website
    - Tags are now pulled from protobufs instead of scrapping local files (allows for tags import without installed steam client)
    '''
}

