"""Persisted GQL query hashes (copied from TwitchDropsMiner constants.py, Oct 2026).
They are refreshed automatically at runtime if Twitch rejects them."""

OPS = {
    "GetStreamInfo": ("VideoPlayerStreamInfoOverlayChannel", "198492e0857f6aedead9665c81c5a06d67b25b58034649687124083ff288597d"),
    "ClaimDrop": ("DropsPage_ClaimDropRewards", "a455deea71bdc9015b78eb49f4acfbce8baa7ccbedd28e549bb025bd0f751930"),
    "Inventory": ("Inventory", "8337eb8541b314040b0edde0c09c5c7a2783ba1960aa9edfbf3bac16d0fec404"),
    "CurrentDrop": ("DropCurrentSessionContext", "4d06b702d25d652afb9ef835d2a550031f1cf762b193523a92166f40ea3d142b"),
    "Campaigns": ("ViewerDropsDashboard", "c16bb890cc8ce7647a96ee69cd313d423a378a3dedadf630a1017cde18975feb"),
    "CampaignDetails": ("DropCampaignDetails", "039277bf98f3130929262cc7c6efd9c141ca3749cb6dca442fc8ead9a53f77c1"),
    "PlaybackAccessToken": ("PlaybackAccessToken", "ed230aa1e33e07eebb8928504583da78a5173989fadfb1ac94be06a04f3cdbe9"),
    "GameDirectory": ("DirectoryPage_Game", "86bcceb4e8b1a51256ff8eed8bd8aae4acacf80d737efe904f84f3aeadf8cafd"),
    "ClaimCommunityPoints": ("ClaimCommunityPoints", "46aaeebe02c99afdf4fc97c7c0cba964124bf6b0af229395f1f6d1feed05b3d0"),
    "ChannelPointsContext": ("ChannelPointsContext", "374314de591e69925fce3ddc2bcf085796f56ebb8cad67a0daa3165c03adc345"),
}
